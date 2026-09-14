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

## 要"动手"的时候，中间会断成两截

查数据是一口气跑完的。但**会改变东西**的动作（重新汇总）不是——
它会跑到一半**停住**，等用户点头：

```
  第一次 invoke ──→ 模型说"我要重新汇总"
                      ↓
                 ★ 框架在这里停住，把"它想干什么"交回来
                      ↓
                界面画出两个按钮，等用户点
                      ↓
  第二次 invoke ──→ 带着用户的答复接着跑
   (Command(resume))     ↓
                    用户点"照做" → 工具真的执行
                    用户点"先别动" → 工具一次都不跑
```

所以有 :func:`ask`（发起）和 :func:`resume`（答复后继续）两个入口，
它们共用同一个 :func:`_run`。中断的内容放在 :class:`AskOutcome` 的
``pending`` 里交回界面。

## 三个必须记住的约束

**一、agent 对象不能建在模块级。** Streamlit 每次交互会把整个脚本从头跑一遍，
模块级的对象会被反复重建（或者更糟：一次建好、之后一直用着旧数据）。
所以每问一次现造一个——造它不联网、不花钱。

**二、对话记忆要存在 session_state 里。** 记忆（checkpointer）要是跟着
agent 一起重建，那每次提问都是"失忆"的，追问"那李四呢"它就不知道你在说谁。
**中断的现场也在记忆里**，所以"停下来等你点头"这件事能跨过 Streamlit 的
重画活下来——上面那张图里两次 ``invoke`` 之间，整个脚本已经重跑了一遍。

**三、工具的收尾工作不能在工具里做。** 工具跑在 LangGraph 的工作线程，
那里没有 Streamlit 的上下文。工具只管查和记，界面的事交给 ui.py。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..llm import BudgetExceeded, LLMClient, LLMError
from .actions import MUTATING_TOOLS, ActionSink, build_rerun_tool
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
class PendingAction:
    """助手想做、但**还没做**的一件事，正等着用户点头。

    这三个字段是框架在中断那一刻交出来的原话：
    ``name`` 是哪个工具，``args`` 是它打算用什么参数，``description`` 是一句说明。
    **必须把 ``args`` 原样带到界面上**——用户要靠它判断"它到底想干什么"。
    界面不该自己编一句"它想重新汇总"，那样用户就没法发现它其实想干别的。
    """

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass
class ActionContext:
    """"动手"类工具要用到的东西，打包成一个。

    **为什么单独打包，不直接塞进 ``build_agent`` 的参数表**：
    这些东西（交接箱、数据库、试跑开关）只有"动手"这一类工具用得上，
    而它们是**可选**的——不传，助手就只能查、不能动，一样能用。
    传了，才多出"重新汇总"这个动作，并且**必然带着停下来问人的保险**。
    """

    #: 工具把新结果放进这里，界面回头来取
    sink: ActionSink = field(default_factory=ActionSink)
    #: 重跑汇总要用它开一次新的运行、记一笔花费
    store: Any = None
    #: 沿用用户上次勾的"不花钱试跑"。**默认 True（不花钱）**——
    #: 万一哪里忘了传，宁可少花钱，也不能悄悄花钱。
    dry_run: bool = True


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
    #: **助手停下来了，在等用户点头**（要做的事列在这里）。
    #:
    #: 这个字段非空时 ``answer`` 一定是空的——它还没做事，当然没有答案。
    #: 界面看到这个就不能显示"AI 没有给出回答"，而要画那张"等你点头"的卡片。
    pending: list[PendingAction] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def waiting(self) -> bool:
        """正在等用户点头。"""
        return bool(self.pending)

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


def _assert_all_actions_gated(tool_names: list[str], interrupt_on: dict[str, Any]) -> None:
    """**每一个"会动东西"的工具，都必须装上"停下来问人"的保险。**

    ## 为什么这是一条断言，而不是一句注释

    漏装的后果是**静默的**：工具照样有、模型照样调、程序照样跑完，
    只是那个"停下来问你"的环节没有了。用户会以为程序问过他，
    其实没有——**这是最坏的一类 bug，因为它看起来一切正常**。

    而它又特别容易漏：加一个新工具时，人很自然地只改工具列表那一行，
    不会想起来还有 ``interrupt_on`` 这个字典要同步。

    所以这里用断言把它焊死。以后谁加了动手工具却忘了登记，
    测试当场就红，而不是等到某天它不问自取地把用户的表重跑了一遍。
    """
    registered = {name for name in tool_names if name in MUTATING_TOOLS}
    missing = registered - set(interrupt_on)
    if missing:
        raise RuntimeError(
            f"这些工具会改变数据，却没有装上「停下来问用户」的保险：{'、'.join(sorted(missing))}。\n"
            "请在 build_agent 的 interrupt_on 里登记它们——"
            "没登记的会自动执行，用户根本不会被问到。"
        )


def build_agent(
    settings: Settings,
    client: LLMClient,
    table: QueryTable,
    ledger: QueryLedger,
    memory: Any,
    actions: ActionContext | None = None,
) -> Any:
    """装配一个问数助手。

    :param memory: 对话记忆。**跨提问复用同一个**，否则追问就接不上。
    :param ledger: 账本。**每次提问换一个新的**，界面只看这一次查了什么。
    :param actions: "动手"类工具需要的东西。**不给（``None``）就只会查不会动**——
        不给的时候连"重新汇总"这个工具都不存在，模型想调也调不到。
    """
    from langchain.agents import create_agent
    from langchain.agents.middleware import (
        HumanInTheLoopMiddleware,
        ModelCallLimitMiddleware,
    )

    tools = [build_query_tool(table, ledger, settings.agent)]
    interrupt_on: dict[str, Any] = {}

    if actions is not None:
        tools.append(
            build_rerun_tool(settings, actions.store, actions.sink, actions.dry_run)
        )
        # ★ 只拦这一个。**没列在这里的工具自动放行**——查数据那种只读操作
        #   不该每问一句都让用户点一次"同意"，那会烦死人，而且点多了
        #   他会开始闭着眼睛点，保险反而失效。
        interrupt_on["rerun_merge"] = {
            "allowed_decisions": ["approve", "reject"],
            # 这句是兜底。真正给用户看的那句话由界面用 args["reason"] 拼——
            # 用户要看的是"它打算干嘛"，不是一句模板话。
            "description": "助手想重新汇总一遍收件箱里的表。",
        }

    _assert_all_actions_gated([t.name for t in tools], interrupt_on)

    return create_agent(
        client.model,
        tools,
        system_prompt=build_system_prompt(table, settings),
        middleware=[
            # 防转圈烧钱。模型偶尔会陷进"查一次不满意再查一次"，
            # 没有上限它能一直试下去。超限时**优雅收尾**（exit_behavior="end"）
            # 而不是抛异常——抛异常的话界面会红一片，用户以为程序坏了。
            ModelCallLimitMiddleware(
                run_limit=settings.agent.max_turns, exit_behavior="end"
            ),
            # "动手先问"。没登记的工具自动放行，所以这条只影响 rerun_merge。
            HumanInTheLoopMiddleware(interrupt_on=interrupt_on),
        ],
        checkpointer=memory,
    )


def _run(
    settings: Settings,
    client: LLMClient,
    table: QueryTable,
    memory: Any,
    payload: Any,
    ledger: QueryLedger,
    actions: ActionContext | None,
    reply: bool = False,
) -> AskOutcome:
    """把一次 ``invoke`` 跑完，然后把结果翻译成 :class:`AskOutcome`。

    **"发起提问"和"答复之后继续"共用这一段。** 两条路除了开头给进去的
    ``payload`` 不一样（一个是新问题，一个是用户的答复），后面全都一样：
    记账、看有没有停下来等人、取答案、兜异常。
    各写一份的话，早晚有一边忘了补记账或忘了兜异常。

    :param reply: 这一次是**用户的答复**（而不是新问题）吗。
    """
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
        agent = build_agent(settings, client, table, ledger, memory, actions)

        # 新问题要先确认上面没有晾着没答复的动作。详见图里那句注释：
        # 不查的话，这一句会被默默吞掉。
        if not reply and _hanging_actions(agent, config):
            outcome.error = (
                "上面那件事还在等你答复——点一下「好，照做」或者「先别动」，"
                "然后我再听你这个问题。"
            )
            return outcome

        result = agent.invoke(payload, config)
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

    # ★ 先看它是不是**停下来等人点头**了。这一段**必须放在取答案之前**。
    #
    # 中断那一刻，最后一条助手消息是"我要调 rerun_merge"——一条**没有文字**
    # 的消息。先取答案的话会取到空，然后报"AI 这次没有给出回答"，
    # 而它明明正在等用户点头。用户看到的是"AI 坏了"，而屏幕上
    # 本该出现的是两个按钮。
    pending = _pending_actions(result)
    if pending:
        outcome.pending = pending
        return outcome

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


def _pending_actions(result: Any) -> list[PendingAction]:
    """从 ``invoke()`` 的返回值里把"等着点头的动作"取出来。

    框架把中断放在 ``result["__interrupt__"]`` 里，形态是
    ``{"action_requests": [{"name", "args", "description"}, ...], ...}``。

    这里的每一步 ``isinstance`` 检查都不是多余的：这段代码跑在
    "模型已经返回、界面还没画"之间，**它抛异常的话用户看到的是白屏**。
    取不到就当没有待批动作，让流程照常往下走。
    """
    actions: list[PendingAction] = []
    for item in result.get("__interrupt__") or []:
        # 中断可能是**对象**（带 ``.value``），也可能直接是**字典**——
        # 实测这一版给的是对象，但字典那种形态在别处出现过。
        # 两种都认：只认一种的话，换成另一种的那天，界面会画不出按钮，
        # 而用户看到的是助手"没有给出回答"——**没有人会想到是解析写窄了**。
        value = getattr(item, "value", None)
        if value is None and isinstance(item, dict):
            value = item.get("value", item)
        if not isinstance(value, dict):
            continue
        for request in value.get("action_requests") or []:
            if not isinstance(request, dict):
                continue
            args = request.get("args")
            actions.append(
                PendingAction(
                    name=str(request.get("name", "")),
                    args=dict(args) if isinstance(args, dict) else {},
                    description=str(request.get("description", "")),
                )
            )
    return actions


def _hanging_actions(agent: Any, config: dict[str, Any]) -> list[PendingAction]:
    """**记忆里还卡着一个没答复的动作吗。**

    为什么要专门查这个——真机上验过：**中断还挂着的时候再问一句，
    新问题会被默默吞掉**。框架不报错，它把消息收进状态、然后原地又回到
    那个中断上。用户看到的是"我问了它，它不理我"，而且**没有任何东西
    能告诉他为什么**。（因为图压根没往下走，工具一次都没跑。）

    界面那边本来就有一道栏杆（有待批动作时不给输入框、把话说明白）。
    这一道是装配层的：**栏杆万一哪天被拆掉或者改坏，
    这里的表现是"它明确告诉你先答复上面那件事"，
    而不是"它吞了你的问题还装作没问过"。**
    """
    try:
        snapshot = agent.get_state(config)
    except Exception:  # noqa: BLE001 - 查不出来就当没有，别把正常提问也堵死
        return []
    interrupts: list[Any] = []
    for task in getattr(snapshot, "tasks", None) or []:
        interrupts.extend(getattr(task, "interrupts", None) or [])
    if not interrupts:
        return []
    return _pending_actions({"__interrupt__": interrupts})


def ask(
    settings: Settings,
    client: LLMClient,
    table: QueryTable,
    question: str,
    memory: Any,
    ledger: QueryLedger | None = None,
    actions: ActionContext | None = None,
) -> AskOutcome:
    """问一句，拿到答案。

    任何失败都**转成** :class:`AskOutcome` 里的 ``error`` 文字，
    不往上抛——界面层要的是"能显示给用户的一句话"，不是堆栈。

    返回值里的 ``pending`` 非空，表示助手**停下来了**，在做之前要用户点头。
    """
    question = question.strip()
    if not question:
        return AskOutcome(error="请先输入一个问题。")

    ledger = ledger if ledger is not None else QueryLedger()
    return _run(
        settings,
        client,
        table,
        memory,
        {"messages": [{"role": "user", "content": question}]},
        ledger,
        actions,
    )


def resume(
    settings: Settings,
    client: LLMClient,
    table: QueryTable,
    memory: Any,
    decisions: list[dict[str, Any]],
    ledger: QueryLedger | None = None,
    actions: ActionContext | None = None,
) -> AskOutcome:
    """用户点完「好，照做」或者「先别动」之后，接着往下走。

    :param decisions: 一个待批动作对应一条答复，**顺序和条数都要对得上**。
        条数不对框架会直接抛 ``ValueError``（实测过：``Number of human
        decisions (0) does not match number of hanging tool calls (1)``），
        所以界面是**按待批动作的个数逐个生成**答复的，不是写死一条。

    这里**重新造了一个 agent**（Streamlit 每次重画都会把一切重建），
    但它和上一次共用同一份 ``memory``，所以能接着上次的中断往下走。
    这一点是实测确认过的——不成立的话，点完按钮就会从头再来一遍。
    """
    from langgraph.types import Command

    ledger = ledger if ledger is not None else QueryLedger()
    if not decisions:
        # 空答复传下去框架会抛 ValueError，用户看到一堆红字却不知道自己做错了什么
        return AskOutcome(ledger=ledger, error="没有收到你的答复，这次先不动了。")

    return _run(
        settings,
        client,
        table,
        memory,
        Command(resume={"decisions": list(decisions)}),
        ledger,
        actions,
        reply=True,
    )


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
    "ActionContext",
    "AskOutcome",
    "PendingAction",
    "ask",
    "build_agent",
    "build_system_prompt",
    "new_memory",
    "resume",
]
