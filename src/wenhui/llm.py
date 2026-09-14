"""LangChain 层：唯一的模型出口。

## 为什么只有这一个文件碰模型

将来想换模型（通义、Kimi、智谱、本地 Ollama），只改这里；
将来要做 v2 的资料问答，也复用这里。**别的地方不许直接 new 模型。**

## 三个必须守住的点

1. **密钥只从环境变量来**（``config.get_api_key``），这里绝不接收也不保存明文密钥。
2. **结构化输出而非解析自然语言**。让模型自由发挥再拿正则去抠 JSON，
   是这类项目最常见的翻车原因。用 Pydantic 约束它必须按格式回答。
3. **花多少钱要算得出来**。每次调用都记账，超上限就停下，
   绝不让"忘了关"变成一张账单。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel

from .config import LLMSettings

T = TypeVar("T", bound=BaseModel)

#: 每百万 token 的价格（人民币）：(输入, 输出)
#: 来源：DeepSeek 官方价格页。**价格会变**，这里只是估算用，不用于精确对账。
#: 查不到名字的模型按 _FALLBACK_PRICE 算，宁可高估也不低估——
#: 让用户看到的钱比实际多，比看到"0 元"结果账单吓一跳要好。
PRICES_CNY_PER_MTOK: dict[str, tuple[float, float]] = {
    "deepseek-flash": (1.0, 2.0),
    "deepseek-v4-pro": (3.0, 6.0),
}
_FALLBACK_PRICE = (1.0, 2.0)


def _ensure_direct_connection(base_url: str) -> str:
    """把模型服务的域名加进「不走代理」名单，返回加进去的域名（没有则空串）。

    ## 为什么非做这件事不可（真机上踩过，很隐蔽）

    这台电脑装了 Clash，用户级环境变量是：

        HTTP_PROXY  = http://127.0.0.1:7890
        NO_PROXY    = localhost,127.0.0.1,::1      <- 没有排除 API 域名

    于是每一次请求 DeepSeek 都被送进 Clash，Clash 再把一个**国内**的 API
    往国外节点上送，节点直接把连接掐掉：

        <urlopen error [WinError 10054] 远程主机强迫关闭了一个现有的连接。>

    **难查的地方在于它在开发机上是好的**——开发用的 shell 里恰好
    ``NO_PROXY`` 多了个 ``api.deepseek.com``，于是怎么试都通；
    用户那边没这个排除项，于是稳定失败。两边现象相反，
    差一点就被当成"偶发网络抖动"糊过去了。

    注意 ``10054``（连接被掐断）和 ``10061``（拒绝连接）要分清：
    代理没在跑是 10061；**代理在跑、但它把请求转坏了，才是 10054**。
    看到 10054 就该先怀疑代理，而不是网线。

    ## 为什么改环境变量，而不是给 httpx 传参数

    代理**同时**影响两条路：自检 ``probe()`` 用 ``urllib``，
    真正的模型调用走 langchain-openai → ``httpx``。
    两个库都读 ``no_proxy`` 环境变量，所以在环境变量这一层改一次，
    两条路一起修好——不用给两个库各写一套绕过逻辑。
    这是进程内的改动，只影响这个程序，不动系统设置、不动用户的 Clash。

    只加 API 这一个域名，不用 ``trust_env=False`` 把代理全关掉：
    万一用户所在网络真的需要代理访问外网，全关掉反而把人弄坏。
    """
    host = urllib.parse.urlsplit(base_url).hostname
    if not host:
        return ""

    changed = False
    for name in ("NO_PROXY", "no_proxy"):
        entries = [e.strip() for e in os.environ.get(name, "").split(",") if e.strip()]
        # 已经写了 "*"（全不走代理）或已经列了这个域名，就别动它
        if "*" in entries or host in entries:
            continue
        entries.append(host)
        os.environ[name] = ",".join(entries)
        changed = True

    return host if changed else ""


class LLMError(RuntimeError):
    """调用模型失败，且信息要说给不懂技术的人听。"""


class BudgetExceeded(LLMError):
    """花费超过设定上限，主动停下。"""


@dataclass
class Usage:
    """用量账本。整个流程共用一个实例，最后汇总给用户看。"""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hits: int = 0          # 走缓存、没花钱的次数
    failures: int = 0
    _model: str = field(default="", repr=False)

    def add_call(self, message: object) -> None:
        """从 LangChain 的 AIMessage 里取 token 用量。取不到就只记次数。"""
        self.calls += 1
        meta = getattr(message, "usage_metadata", None)
        if isinstance(meta, dict):
            self.input_tokens += int(meta.get("input_tokens") or 0)
            self.output_tokens += int(meta.get("output_tokens") or 0)

    def cost_cny(self, model: str) -> float:
        rate_in, rate_out = PRICES_CNY_PER_MTOK.get(model, _FALLBACK_PRICE)
        return (
            self.input_tokens / 1_000_000 * rate_in
            + self.output_tokens / 1_000_000 * rate_out
        )

    def describe(self, model: str) -> str:
        cost = self.cost_cny(model)
        parts = [f"调用模型 {self.calls} 次"]
        if self.cache_hits:
            parts.append(f"命中缓存 {self.cache_hits} 次（没花钱）")
        if self.failures:
            parts.append(f"失败 {self.failures} 次")
        parts.append(f"约花费 {cost:.4f} 元")
        return "，".join(parts)


class LLMClient:
    """按需创建模型、调用、记账。"""

    def __init__(self, settings: LLMSettings, api_key: str, cost_limit_cny: float = 0.0) -> None:
        self.settings = settings
        self._api_key = api_key
        self.cost_limit_cny = cost_limit_cny
        self.usage = Usage()
        self._model = None          # 懒加载：没真用上就不建连接
        self._structured_method: str | None = self._UNSET   # 记住哪种结构化方式能用

        # 在建任何连接**之前**把代理绕开。放在这里是因为自检（urllib）
        # 和真正的模型调用（httpx）都必须先经过这个构造函数，
        # 一处调用两条路都受益。详见 _ensure_direct_connection。
        self.direct_host = ""
        if settings.bypass_proxy:
            self.direct_host = _ensure_direct_connection(settings.base_url)

    # ------------------------------------------------------------------ 模型

    @property
    def model(self):
        if self._model is None:
            self._model = self._build_model()
        return self._model

    def _build_model(self):
        try:
            from langchain.chat_models import init_chat_model
        except ImportError:  # 兼容旧版路径
            from langchain.chat_models import init_chat_model  # type: ignore

        extra: dict = {}
        if self.settings.disable_thinking:
            extra["extra_body"] = {"thinking": {"type": "disabled"}}

        return init_chat_model(
            model=self.settings.model,
            model_provider="openai",
            base_url=self.settings.base_url,
            api_key=self._api_key,
            temperature=self.settings.temperature,
            timeout=self.settings.timeout_seconds,
            max_retries=0,          # 重试我们自己控制，方便记账和给用户看进度
            **extra,
        )

    # ------------------------------------------------------------------ 调用

    def structured(self, schema: type[T], system: str, user: str) -> T:
        """要模型按 ``schema`` 的格式回答，返回 Pydantic 对象。

        ``include_raw=True`` 是必要的：模型返回的原始消息里带着 token 用量，
        只拿解析结果的话就没法记账了。

        失败会重试；重试仍失败就抛 ``LLMError``，由调用方决定降级方案。
        """
        self._check_budget()

        last_error: Exception | None = None
        for attempt in range(1, self.settings.max_retries + 1):
            try:
                result = self._invoke_structured(schema, system, user)
                if isinstance(result, dict):
                    raw = result.get("raw")
                    parsed = result.get("parsed")
                    if raw is not None:
                        self.usage.add_call(raw)
                    if parsed is not None:
                        return parsed
                    last_error = LLMError(f"模型没有按要求格式回答：{result.get('parsing_error')}")
                elif isinstance(result, schema):
                    return result
                else:
                    last_error = LLMError("模型返回了预期之外的内容。")
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络/鉴权/格式各种错都要兜住
                last_error = exc
                self.usage.failures += 1
                if attempt < self.settings.max_retries:
                    # 退避等待：API 偶发抽风，等一下通常就好了
                    time.sleep(min(2 ** (attempt - 1), 8))

        raise LLMError(self._friendly_error(last_error))

    #: 结构化输出的尝试顺序。**顺序是实测出来的，别随便改**：
    #:
    #: 1. ``function_calling`` —— 服务端强制模型只能从给定字段里挑，
    #:    这是"防止模型编造字段名"最硬的一道锁。DeepSeek 支持。
    #: 2. ``json_mode`` —— 只保证返回合法 JSON，字段名靠提示词约束，
    #:    锁松了一层，但比没有强。
    #: 3. 默认方式（原生 json_schema）—— 约束最紧，可惜 DeepSeek
    #:    目前回 ``This response_format type is unavailable now``。
    #:    放最后是因为对别家服务（OpenAI、通义）它往往才是最优解。
    _METHODS: tuple[str | None, ...] = ("function_calling", "json_mode", None)

    #: 用于记住"哪种方式成功过"。``None`` 是合法的方法名，所以不能拿它当"还没试过"。
    _UNSET = "\x00unset"

    def _invoke_structured(self, schema: type[T], system: str, user: str):
        """按 schema 要结构化结果，在几种传参方式之间自动兜底。

        把 Pydantic schema 交给服务端有多种方式，不同厂商、甚至同一厂商的
        不同模型支持程度都不一样（DeepSeek 就变过）。**一旦某种方式成功过
        就记住它**，后面不再白试失败的那些。
        """
        messages = [("system", system), ("human", user)]
        if self._structured_method == self._UNSET:
            methods: list[str | None] = list(self._METHODS)
        else:
            methods = [self._structured_method]  # type: ignore[list-item]

        errors: list[str] = []
        for method in methods:
            try:
                kwargs: dict = {"include_raw": True}
                if method:
                    kwargs["method"] = method
                runnable = self.model.with_structured_output(schema, **kwargs)
                result = runnable.invoke(messages)
                self._structured_method = method  # type: ignore[assignment]
                return result
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(f"[{method or '默认方式'}] {exc}")

        raise LLMError(
            "结构化输出失败，几种方式都不行：\n  " + "\n  ".join(errors)
        )

    def _check_budget(self) -> None:
        if self.cost_limit_cny <= 0:
            return
        cost = self.usage.cost_cny(self.settings.model)
        if cost > self.cost_limit_cny:
            raise BudgetExceeded(
                f"这次运行的花费已经到 {cost:.2f} 元，超过了你设的上限 "
                f"{self.cost_limit_cny:.2f} 元，程序主动停下了。\n"
                "如果这是正常的，打开 config/settings.toml，把 cost_limit_cny 调大即可。"
            )

    def _friendly_error(self, exc: Exception | None) -> str:
        text = str(exc) if exc else "未知原因"
        lowered = text.lower()
        hints: list[str] = []
        if "401" in text or "authentication" in lowered or "invalid api key" in lowered:
            hints.append("看起来是密钥不对。请重新运行「设置密钥.bat」再填一次。")
        elif "402" in text or "insufficient" in lowered or "balance" in lowered:
            hints.append("看起来是账户余额不足。请到 platform.deepseek.com 充值。")
        elif "model" in lowered and ("not found" in lowered or "not exist" in lowered):
            hints.append(
                "看起来是模型名不对。DeepSeek 改过模型名——"
                "打开 config/settings.toml，把 model 改成程序自检里列出的可用名字。"
            )
        elif "timeout" in lowered or "timed out" in lowered:
            hints.append("看起来是网络超时。可以重试一次，或在设置里把 timeout_seconds 调大。")
        elif "connection" in lowered:
            hints.append("看起来网络连不上。请检查网络，或确认没有开代理拦截。")

        message = f"调用大模型失败：{text}"
        if hints:
            message += "\n\n" + "\n".join(hints)
        return message

    # ------------------------------------------------------------------ 自检

    def probe(self) -> tuple[bool, str]:
        """启动自检：确认密钥能用、以及**当前有哪些模型可用**。

        这一步很有必要——DeepSeek 在 2026-07-24 下线了 ``deepseek-chat``。
        如果配置里写的模型名已经不存在，自检要当场告诉用户该改成什么，
        而不是等他点了"开始汇总"才报一个看不懂的错。
        """
        url = self.settings.base_url.rstrip("/") + "/models"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._api_key}"}
        )

        # 连不上要重试几次再下结论。**这不是多余的**：国内网络上偶发的
        # 连接被掐（WinError 10054）很常见，试第二次通常就通了。
        # 而这个自检的结果界面会缓存 5 分钟——不重试的话，抖一下就要挂
        # 5 分钟的"AI 现在用不了"，用户会以为程序坏了。
        #
        # 只对"连接类"错误重试。HTTP 层能应答（401、400）说明连接是好的，
        # 重试一百次也还是同样的结果，白等。
        payload = None
        last_error: Exception | None = None
        attempts = max(2, self.settings.max_retries)
        for attempt in range(1, attempts + 1):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.settings.timeout_seconds
                ) as resp:
                    payload = json.load(resp)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    return False, "密钥无效。请重新运行「设置密钥.bat」。"
                return False, f"自检失败：服务返回 {exc.code}。"
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < attempts:
                    time.sleep(min(2 ** (attempt - 1), 4))

        if payload is None:
            return False, (
                f"自检失败：{attempts} 次都没连上模型服务（{last_error}）。\n\n"
                "最常见的原因：电脑上的代理软件（Clash / VPN）把这个请求"
                "拦掉了。程序已经自动让 API 域名不走代理，如果还是不行，"
                "试着把代理软件先关掉再启动。"
            )

        available = sorted(
            str(item.get("id", ""))
            for item in payload.get("data", [])
            if isinstance(item, dict) and item.get("id")
        )
        if not available:
            return True, "连上了模型服务，但没能列出可用模型（不影响使用）。"

        if self.settings.model in available:
            return True, f"连接正常，正在使用 {self.settings.model}。"

        return False, (
            f"配置里写的模型「{self.settings.model}」在当前账号下不存在。\n\n"
            f"可用的模型有：{'、'.join(available)}\n\n"
            "请打开 config/settings.toml，把 model 改成上面其中一个。"
        )
