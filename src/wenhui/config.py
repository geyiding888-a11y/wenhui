"""配置读取。

两条规矩，请勿破坏：

1. **密钥只从环境变量读**（``LLM_API_KEY``），绝不写入任何文件。
   ``.env.example`` 里只有变量名，真实值永远不进仓库。
2. 其余设置从 ``config/settings.toml`` 读，用户可以自己改。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# src/wenhui/config.py -> 上溯三层就是项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[2]

SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.toml"
INBOX_DIR = PROJECT_ROOT / "收件箱"
TEMPLATE_DIR = PROJECT_ROOT / "模板"
OUTPUT_DIR = PROJECT_ROOT / "输出"
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DB = DATA_DIR / "wenhui.db"

#: 密钥的环境变量名。只在这里出现一次，改就全改。
API_KEY_ENV = "LLM_API_KEY"


class ConfigError(RuntimeError):
    """配置有问题，且是用户能看懂、能自己修的那种。"""


@dataclass(frozen=True)
class LLMSettings:
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com/v1"
    temperature: float = 0.0
    timeout_seconds: int = 60
    max_retries: int = 3
    #: 关掉模型的"思考"（推理）模式。**默认开着这个开关，是有原因的**：
    #: deepseek-flash 这类会思考的模型，在思考模式下服务端不允许
    #: "强行指定用哪个工具"，而我们要的强约束结构化输出正好靠这个。
    #: 列名对齐是判断题不是推理题，关掉思考不损失准确度，还更快更省。
    #: 换别家模型服务时若它不认这个参数，把 settings.toml 里这项改成 false。
    disable_thinking: bool = True
    #: 让模型服务的域名**不走系统代理**。**默认开着，别关**。
    #: 这台电脑装了 Clash（HTTP_PROXY=127.0.0.1:7890），而 NO_PROXY 里
    #: 没排除 API 域名，于是请求被送进 Clash、再往国外节点上送，
    #: 节点直接把连接掐了：WinError 10054。国内 API 走国外节点，本来就
    #: 又慢又容易断。详见 llm.py 里 _ensure_direct_connection 的说明。
    #: 万一你的学校网络**必须**通过代理才能访问外网，把这项改成 false。
    bypass_proxy: bool = True


@dataclass(frozen=True)
class AggregateSettings:
    confidence_threshold: float = 0.75
    samples_per_column: int = 5
    max_files_per_run: int = 100
    cost_limit_cny: float = 5.0


@dataclass(frozen=True)
class PrivacySettings:
    mask_id_card: bool = True
    mask_phone: bool = True
    mask_bank_card: bool = True
    mask_email: bool = True


@dataclass(frozen=True)
class ExcelSettings:
    header_scan_rows: int = 10
    treat_as_empty: tuple[str, ...] = (
        "无", "没有", "暂无", "—", "-", "/", "N/A", "NA", "null", "NULL", "None", "",
    )


@dataclass(frozen=True)
class AgentSettings:
    """对话框（问数助手）的设置。

    这些开关只管**问答**那一个功能，不影响汇总流水线。
    """

    #: 不想要对话框就改成 false，界面上那一块会整块消失。
    enabled: bool = True
    #: 一次提问最多让模型来回几轮（它可能要查好几次才答得上来）。
    #: 上限的作用是**防转圈烧钱**：模型偶尔会陷进"查一次不满意再查一次"，
    #: 没有上限的话它会一直试下去。
    max_turns: int = 6
    #: 查询结果最多回传多少行给模型。
    #: 超过的部分只告诉它"一共多少行"，不把全部内容发出去——
    #: 既省 token，也少往外发数据。
    result_rows_for_ai: int = 20
    #: 给模型看的每列样本个数。给 0 也行（只看列名它就能写查询），
    #: 但给两三个能让它更清楚这列是"数字"还是"文字"。
    samples_per_column: int = 3
    #: 要不要把**姓名**也打码。**默认不打（false）**，理由：
    #: 打码了"张三有多少学生"这类问题就永远答不出来，而这个功能的价值
    #: 一大半就在这儿。而且现有汇总流程本来就会把姓名样本发给模型
    #: （对齐列名时），这里不打码并没有**新增**泄露面。
    #:
    #: 但确实变了：以前模型只看得到"列名 + 几个打码样本"，
    #: 现在它会看到查询结果里一行行的真实姓名。**介意就改成 true**——
    #: 改完成绩单那类问题照样能答，只是回答里的人都变成"张*"。
    mask_name_in_answer: bool = False

    #: 问答的累计花费上限（元）。0 表示不限制。
    #:
    #: **为什么问答要单独一个上限，不跟汇总共用一个**：两者的花法完全不同。
    #: 汇总是"点一次花一笔"，一次几毛钱，你心里有数；问答是"打一句话花一点"，
    #: 一次几分钱，但**可以问一百次**。共用一个额度的话，你问着问着
    #: 突然发现汇总跑不了了，而且说不清钱花在哪。
    #:
    #: 这个数是**累计**的（存在数据库里，关掉程序再打开也还算数），
    #: 不是"每问一次给这么多"。
    cost_limit_cny: float = 2.0


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings = field(default_factory=LLMSettings)
    aggregate: AggregateSettings = field(default_factory=AggregateSettings)
    privacy: PrivacySettings = field(default_factory=PrivacySettings)
    excel: ExcelSettings = field(default_factory=ExcelSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)


def load_settings(path: Path | None = None) -> Settings:
    """读 ``config/settings.toml``；文件不存在或某项缺失时用默认值，不报错。

    环境变量 ``LLM_MODEL`` / ``LLM_BASE_URL`` 优先级最高——
    临时想换个模型试一下，不用改文件。
    """
    path = path or SETTINGS_PATH
    raw: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

    def section(name: str) -> dict:
        value = raw.get(name, {})
        return value if isinstance(value, dict) else {}

    llm_raw = section("llm")
    llm = LLMSettings(
        model=os.environ.get("LLM_MODEL") or llm_raw.get("model", LLMSettings.model),
        base_url=os.environ.get("LLM_BASE_URL") or llm_raw.get("base_url", LLMSettings.base_url),
        temperature=float(llm_raw.get("temperature", LLMSettings.temperature)),
        timeout_seconds=int(llm_raw.get("timeout_seconds", LLMSettings.timeout_seconds)),
        max_retries=int(llm_raw.get("max_retries", LLMSettings.max_retries)),
        disable_thinking=bool(
            llm_raw.get("disable_thinking", LLMSettings.disable_thinking)
        ),
        bypass_proxy=bool(llm_raw.get("bypass_proxy", LLMSettings.bypass_proxy)),
    )

    agg_raw = section("aggregate")
    aggregate = AggregateSettings(
        confidence_threshold=float(
            agg_raw.get("confidence_threshold", AggregateSettings.confidence_threshold)
        ),
        samples_per_column=int(
            agg_raw.get("samples_per_column", AggregateSettings.samples_per_column)
        ),
        max_files_per_run=int(agg_raw.get("max_files_per_run", AggregateSettings.max_files_per_run)),
        cost_limit_cny=float(agg_raw.get("cost_limit_cny", AggregateSettings.cost_limit_cny)),
    )

    priv_raw = section("privacy")
    privacy = PrivacySettings(
        mask_id_card=bool(priv_raw.get("mask_id_card", True)),
        mask_phone=bool(priv_raw.get("mask_phone", True)),
        mask_bank_card=bool(priv_raw.get("mask_bank_card", True)),
        mask_email=bool(priv_raw.get("mask_email", True)),
    )

    excel_raw = section("excel")
    excel = ExcelSettings(
        header_scan_rows=int(excel_raw.get("header_scan_rows", ExcelSettings.header_scan_rows)),
        treat_as_empty=tuple(excel_raw.get("treat_as_empty", ExcelSettings.treat_as_empty)),
    )

    agent_raw = section("agent")
    agent = AgentSettings(
        enabled=bool(agent_raw.get("enabled", AgentSettings.enabled)),
        max_turns=int(agent_raw.get("max_turns", AgentSettings.max_turns)),
        result_rows_for_ai=int(
            agent_raw.get("result_rows_for_ai", AgentSettings.result_rows_for_ai)
        ),
        samples_per_column=int(
            agent_raw.get("samples_per_column", AgentSettings.samples_per_column)
        ),
        mask_name_in_answer=bool(
            agent_raw.get("mask_name_in_answer", AgentSettings.mask_name_in_answer)
        ),
        cost_limit_cny=float(
            agent_raw.get("cost_limit_cny", AgentSettings.cost_limit_cny)
        ),
    )

    return Settings(llm=llm, aggregate=aggregate, privacy=privacy, excel=excel, agent=agent)


# --------------------------------------------------------------------------
# 密钥：只读环境变量，绝不落盘
# --------------------------------------------------------------------------


def get_api_key() -> str | None:
    """从环境变量取密钥。取不到返回 ``None``（不抛异常，交给界面去引导用户）。"""
    key = os.environ.get(API_KEY_ENV, "").strip()
    return key or None


def describe_api_key() -> str:
    """给界面显示用的密钥状态——**只露最后 4 位**，其余打码。

    这样用户能确认"我设的是哪把钥匙"，而截屏、录屏、日志都不会泄露密钥。
    """
    key = get_api_key()
    if not key:
        return "未设置"
    if len(key) <= 4:
        return "已设置（太短，可能不对）"
    return f"已设置（尾号 {key[-4:]}，共 {len(key)} 位）"


def require_api_key() -> str:
    """需要真调用模型时用这个；没设密钥就抛出人话错误。"""
    key = get_api_key()
    if not key:
        raise ConfigError(
            "还没有设置 API 密钥，程序没法调用大模型。\n"
            "解决办法：双击运行「设置密钥.bat」（Mac 上是「设置密钥.command」），\n"
            "把 DeepSeek 的密钥粘贴进去，然后重新启动程序。"
        )
    return key
