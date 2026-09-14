"""把模型、工具、记忆装配成一个"能问话的助手"。

## 一次提问发生了什么

```
  你打字："乙老师这学期要上多少课？"
        ↓
  agent 把问题 + 表结构说明 + 对话历史 发给模型
        ↓
  模型说："我要调 query_records，SQL 是 SELECT ... WHERE 姓名='乙老师'"
        ↓
  ★ 工具在工作线程里跑（只读闸 + 超时 + 打码）
        ↓
  模型拿到结果，说人话："乙老师本学期课时为 128。"
        ↓
  这一轮的 token 用量进账（否则界面上的花费永远是 0）
```

## 三个必须记住的约束

**一、agent 对象不能建在模块级。** Streamlit 每次交互会把整个脚本从头跑一遍，
模块级的对象会被反复重建（或者更糟：一次建好、之后一直用着旧数据）。
所以每问一次现造一个——造它不联网、不花钱。

**二、对话记忆要存在 session_state 里。** 记忆（checkpointer）要是跟着
agent 一起重建，那每次提问都是"失忆"的，追问"那李四呢"它就不知道你在说谁。

**三、工具的收尾工作不能在工具里做。** 工具跑在 LangGraph 的工作线程，
那里没有 Streamlit 的上下文。工具只管查和记，界面的事交给 ui.py。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..llm import BudgetExceeded, LLMClient, LLMError
from .table import QueryTable
from .tools import QueryLedger, build_query_tool

#: 对话记忆的线程名。只有一条对话线，所以是个固定值。
#: 以后要是想支持"开好几个话题分别聊"，改这里成动态的即可。
THREAD_ID = "wenhui-ask"

#: 提示词里那条最要紧的规矩，单独拎出来是为了让测试能钉住它。
#: **模型很爱"根据常识推断"一个数字出来**，而用户完全没法分辨
#: 那个数字是查出来的还是编的——这是整个功能可信度的命根子。
NO_GUESSING_RULE = "绝对不许凭印象、凭常识、凭推测给出数字或名单"


def build_system_prompt(table: QueryTable, settings: Settings) -> str:
    """给模型写"你是谁、要守什么规矩、这表长什么样"。

    表结构说明（列名、类型、脱敏后的样本）直接拼在这里，
    这样模型每次提问都看得到它有哪些列可用。
    """
    schema = table.schema_text(settings.agent.samples_per_column)

    # 有数据、没数据，说法不一样。没数据时得让它直接说"还没汇总"，
    # 而不是硬着头皮写一条查不到东西的 SQL 然后编一个答案。
    if table.n_rows:
        data_note = f"当前表里有 {table.n_rows} 行数据。"
    else:
        data_note = (
            "**当前表里一行数据都没有**（用户还没跑过汇总）。"
            "如果被问到数据，请直接告诉他先去点「开始汇总」，不要编造任何内容。"
        )

    return f"""你是一个高校资料汇总系统的问数助手。用户是教务老师，不是技术人员。

## 你的职责

把用户的问题翻译成一条查询，查完用**简体中文**把结果说给他听。

## 最要紧的一条规矩

**{NO_GUESSING_RULE}。**
任何关于人数、课时、职称、名单的问题，都必须先调用 query_records 查出来再说。
不许说"根据常识应该是……"，不许说"大概是……"，不许自己算。
如果查不到，就如实说"表里没有找到"，**这比编一个数字出来强一百倍**。
用户没法分辨你给的是查出来的还是编的——所以这条没有例外。

## 回答的写法

- 一律用简体中文。
- 直接给答案，别复述你写了什么 SQL（用户看不懂，也不关心）。
- 数字要带上下文，比如"乙老师本学期课时为 128"，而不是干巴巴一个 "128"。
- 如果结果里有很多行，挑重点说，或者帮他归纳一下，别把 20 行原样念一遍。
- **"排在前几名"的回答必须交代并列情况。** 比如"最多的前 3 位是谁"，
  如果实际上有 5 个人并列最多，只说那 3 个名字就是在误导——用户会以为
  最多的只有这 3 个人。要如实说清楚："最多的都是 180 人，一共有 5 位，
  分别是……"。**宁可多报几个人，也不能漏掉并列的人。**
- 拿不准就问一句，别猜。

## 表结构

{schema}

{data_note}

## 你能做的事

只有一个工具：query_records（查数据）。**你不能修改任何数据。**
如果用户要求你改数据、删文件、重新汇总，告诉他这需要他自己在界面上操作。"""


# --------------------------------------------------------------------------
# 记账：必须用回调，不能从返回的消息里数
# --------------------------------------------------------------------------


def _make_usage_recorder(client: LLMClient) -> Any:
    """造一个回调，**每一次模型调用结束时**记一笔账。

    ## 为什么不能简单地从返回值里数消息

    开了对话记忆之后，``agent.invoke()`` 返回的 ``messages`` 是**整条对话线
    从头到现在的全部消息**，不是这一轮的。照着它遍历记账，等于把前面每一轮
    都重新算一遍钱——问第十个问题时，花费会显示成前十次的总和再乘几倍。

    这个 bug 很阴：数字看起来"变大了"，你会以为只是贵，而不会想到是重复计数。

    回调是**按真实发生的模型调用**触发的，一次调用一次 ``on_llm_end``，
    和对话有多长完全无关。用这个才对。
    """
    from langchain_core.callbacks import BaseCallbackHandler

    class _UsageRecorder(BaseCallbackHandler):
        def __init__(self) -> None:
            self.recorded = 0

        def on_llm_end(self, response: Any, **kwargs: Any) -> None:
            # 一次调用可能返回多个候选（n>1），都要记
            for generation_list in getattr(response, "generations", []) or []:
                for generation in generation_list:
                    message = getattr(generation, "message", None)
                    if client.record_usage(message):
                        self.recorded += 1

        def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
            client.record_failure()

    return _UsageRecorder()


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------


@dataclass
class AskOutcome:
    """一次提问的结果。"""

    #: 助手的回答（已经是一段人话）
    answer: str = ""
    #: 这次问了什么、查到了什么。界面用它展示"AI 到底查了什么"
    ledger: QueryLedger = field(default_factory=QueryLedger)
    #: 这一轮记上了几笔账
    n_recorded: int = 0
    #: 这一轮花了多少（元）
    cost_cny: float = 0.0
    #: 出错时的说明。空字符串表示正常
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def usage_uncounted(self) -> bool:
        """**钱花了但没记上账。**

        界面上要能看出来——不然用户看到一个偏小的数字，会以为这功能便宜。
        只在"确实调过模型、却一笔都没记上"时才为真。
        """
        return self.ok and bool(self.answer) and self.n_recorded == 0


# --------------------------------------------------------------------------
# 装配与提问
# --------------------------------------------------------------------------


def new_memory() -> Any:
    """造一份空的对话记忆。

    **必须存在 ``st.session_state`` 里**，不然每次界面刷新都是一次失忆。
    """
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


def build_agent(
    settings: Settings,
    client: LLMClient,
    table: QueryTable,
    ledger: QueryLedger,
    memory: Any,
) -> Any:
    """装配一个问数助手。

    :param memory: 对话记忆。**跨提问复用同一个**，否则追问就接不上。
    :param ledger: 账本。**每次提问换一个新的**，界面只看这一次查了什么。
    """
    from langchain.agents import create_agent
    from langchain.agents.middleware import ModelCallLimitMiddleware

    tool = build_query_tool(table, ledger, settings.agent)

    return create_agent(
        client.model,
        [tool],
        system_prompt=build_system_prompt(table, settings),
        middleware=[
            # 防转圈烧钱。模型偶尔会陷进"查一次不满意再查一次"，
            # 没有上限它能一直试下去。超限时**优雅收尾**（exit_behavior="end"）
            # 而不是抛异常——抛异常的话界面会红一片，用户以为程序坏了。
            ModelCallLimitMiddleware(
                run_limit=settings.agent.max_turns, exit_behavior="end"
            )
        ],
        checkpointer=memory,
    )


def ask(
    settings: Settings,
    client: LLMClient,
    table: QueryTable,
    question: str,
    memory: Any,
    ledger: QueryLedger | None = None,
) -> AskOutcome:
    """问一句，拿到答案。

    任何失败都**转成** :class:`AskOutcome` 里的 ``error`` 文字，
    不往上抛——界面层要的是"能显示给用户的一句话"，不是堆栈。
    """
    question = question.strip()
    if not question:
        return AskOutcome(error="请先输入一个问题。")

    ledger = ledger if ledger is not None else QueryLedger()
    outcome = AskOutcome(ledger=ledger)

    # 超预算就别开新一轮了。问数助手绕过了 structured()，
    # 不在这儿查一次的话，settings.toml 里的花费上限对它完全无效。
    try:
        client.check_budget()
    except BudgetExceeded as exc:
        outcome.error = str(exc)
        return outcome

    recorder = _make_usage_recorder(client)
    config: dict[str, Any] = {
        "configurable": {"thread_id": THREAD_ID},
        "callbacks": [recorder],
    }

    try:
        agent = build_agent(settings, client, table, ledger, memory)
        result = agent.invoke(
            {"messages": [{"role": "user", "content": question}]}, config
        )
    except BudgetExceeded as exc:
        outcome.error = str(exc)
        return outcome
    except LLMError as exc:
        outcome.error = str(exc)
        return outcome
    except Exception as exc:  # noqa: BLE001 - 界面层兜底，绝不能让白屏
        outcome.error = f"这次没问成：{exc}"
        return outcome

    outcome.n_recorded = recorder.recorded
    outcome.cost_cny = client.cost_so_far()

    # 取最后一条**有文字内容**的助手消息。
    # 不能只取最后一条消息：模型可能在最后还在发工具调用请求，
    # 那条消息没有文字，直接取会得到一个空答案。
    for message in reversed(result.get("messages", [])):
        if _is_ai_text(message):
            outcome.answer = _text_of(message)
            break

    if not outcome.answer:
        outcome.error = (
            "AI 这次没有给出回答。可以换个问法再试一次；"
            "如果反复这样，说明问题可能太复杂了。"
        )
    return outcome


def _is_ai_text(message: Any) -> bool:
    """这条消息是不是"助手说的一段话"（而不是工具调用请求 / 工具返回）。"""
    if getattr(message, "type", "") != "ai":
        return False
    content = getattr(message, "content", "")
    if not content:
        return False
    # 有的模型把内容拆成块，纯工具调用的块里没有文字
    if isinstance(content, list):
        return any(
            isinstance(block, str)
            or (isinstance(block, dict) and block.get("type") == "text" and block.get("text"))
            for block in content
        )
    return True


def _text_of(message: Any) -> str:
    """把一条助手消息的文字内容取出来（可能被拆成了好几块）。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("text"):
            parts.append(str(block["text"]))
    return "\n".join(parts).strip()


__all__ = [
    "NO_GUESSING_RULE",
    "THREAD_ID",
    "AskOutcome",
    "ask",
    "build_agent",
    "build_system_prompt",
    "new_memory",
]
