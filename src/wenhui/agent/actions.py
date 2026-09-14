"""AI 能"动手"做的事。**这个包里唯一会让东西发生变化的文件。**

## 为什么单独一个文件，不跟查询工具放一起

``tools.py`` 里的东西清一色是**只读**的——查一下、看一眼、什么也不改。
那里可以放心地"自动执行、不打扰用户"，因为它再怎么样也改不坏东西。

这个文件里的东西不一样：它**真的会动数据、真的会花钱**。
把它单独放，是为了让这条界线在文件层面就看得见——
以后有人要加"删掉重复行""改一列的值"这类操作，
他打开这个文件，开头这句话就在眼前。

## 铁律：这里每加一个工具，都必须登记进"保险"

``MUTATING_TOOLS`` 是"会动东西的工具"名单，``chat.py`` 拿它来断言
"每一个都装上了停下来问人的保险"。**新加一个动手工具却忘了登记，
测试当场就会红**——而不是等到某天它不问自取地把用户的表重跑了一遍。

这就是"先装保险，再给枪上子弹"那句话的机械化版本：
不靠记性，靠断言。

## 重跑为什么不能自己做主

"重新汇总"听起来无害，其实有三个后果用户未必想到：

1. **会花钱**（除非上次勾了"不花钱试跑"）
2. **会覆盖他看到的总表**——他可能正对着那张表在核对
3. **用的是此刻收件箱里的文件**——他可能刚往里塞了几个新表

所以这件事**必须停下来问**。而且停下来的那一刻要告诉他这三件事，
不然那个"好，照做"的按钮就是在让他闭着眼睛签字。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Settings

#: 重跑汇总这个工具的名字。
#:
#: **这个名字必须和 ``interrupt_on`` 里的键一模一样**，差一个字，
#: 保险就装到空气上了——工具照跑，一次都不问。所以它只在这里定义一次，
#: 别处一律引用这个常量，不手抄字符串。
RERUN_TOOL_NAME = "rerun_merge"

#: 所有**会改变东西**的工具名。``chat.py` 用它来断言保险没漏装。
MUTATING_TOOLS = frozenset({RERUN_TOOL_NAME})


@dataclass
class ActionSink:
    """工具 → 界面的交接箱。

    **为什么需要这么个东西**：工具跑在 LangGraph 的**工作线程**里，
    那里碰不到 ``st.session_state``（碰了会抛 ``missing ScriptRunContext``）。
    而重跑汇总产出的新结果，最后是要交给界面去画的。

    所以工具只能把结果先放进这个箱子里，等 ``invoke()`` 回到主线程，
    界面再来开箱。和 ``tools.py`` 里 :class:`~wenhui.agent.tools.QueryLedger`
    是同一个套路——**工具只写纯对象，界面的事界面自己做**。
    """

    #: 重跑产出的新汇总结果。没重跑过就是 ``None``
    new_result: Any = None
    #: 重跑失败时的说明
    error: str = ""
    #: 到底跑没跑。**"跑了但失败"和"压根没跑"要分得清**——
    #: 混在一起的话，用户点了"照做"却什么都没发生，界面会显示成正常
    ran: bool = False

    @property
    def produced(self) -> bool:
        return self.new_result is not None


def run_rerun(
    settings: Settings,
    store: Any,
    sink: ActionSink,
    dry_run: bool,
    reason: str,
) -> str:
    """真的重跑一遍汇总，返回**给模型看的那段文字**。

    单独拆出来（不写在 ``@tool`` 里面），是为了能**脱离 LangChain 直接测**——
    工具的外壳是框架的事，"重跑怎么跑、出了事怎么说"是我们的事。

    **任何失败都返回文字，不往上抛。** 抛出去的话，用户点完"照做"会看到
    一整页红色堆栈，而他只是想让程序重跑一遍而已。

    :param dry_run: 沿用用户上次勾的"不花钱试跑"。**这一条不能省**——
        他上次勾了试跑、这次让助手重跑，如果不沿用，点一下"好，照做"
        就**悄悄花了钱**。他完全看不出来，因为界面上的按钮并没有提过钱。
    """
    from .. import pipeline
    from ..config import OUTPUT_DIR

    sink.ran = True

    try:
        result = pipeline.run(
            # ``files=None`` 表示"重新扫一遍收件箱"。这是用户说"重新汇总"
            # 时的本意——他可能刚往收件箱里补了几个单位的表。
            files=None,
            template=pipeline.find_template(),
            settings=settings,
            store=store,
            dry_run=dry_run,
            output_dir=OUTPUT_DIR,
        )
    except pipeline.PipelineError as exc:
        # 这是"收件箱是空的"这类能说清楚的毛病，原话转给模型
        sink.error = str(exc)
        return (
            f"重跑没能完成：{exc}\n"
            "请把这句话如实告诉用户，不要编一个结果出来。"
        )
    except Exception as exc:  # noqa: BLE001 - 兜底，绝不让堆栈冲到界面上
        sink.error = f"重跑的时候出了意外：{exc}"
        return (
            f"重跑的时候出了意外：{exc}\n"
            "请如实告诉用户这次没成功，不要编一个结果出来。"
        )

    sink.new_result = result

    n_records = len(result.records)
    n_issues = len(result.issues)
    note = (
        "（这次是不花钱试跑，列名靠名称相似度对齐，可能没上次准）"
        if dry_run
        else ""
    )
    return (
        f"已经重新汇总完成：{n_records} 条记录，{n_issues} 条待确认的问题。{note}\n"
        "新的总表已经自动更新在用户界面上了。\n"
        "请用一句简体中文告诉用户「已经重新汇总好了」，说清楚一共多少条记录、"
        "有多少条待确认。**不要念细节**，也不要复述这份说明。"
    )


def build_rerun_tool(
    settings: Settings,
    store: Any,
    sink: ActionSink,
    dry_run: bool,
) -> Any:
    """造一个"重新汇总"工具。

    :returns: 一个 LangChain 工具对象。**放进 ``create_agent`` 之前，
        必须确认它已经在 ``interrupt_on`` 里登记过**（``chat.py`` 会断言）。
    """
    from langchain_core.tools import tool

    # 这段说明是写给模型看的，重点只有一个：**别没事就重跑**。
    # 它很爱"顺手把数据刷新一下"——而这里刷一次要花钱、要覆盖用户
    # 正在看的表。不写死这句话，用户问"乙老师多少课时"它都有可能去重跑。
    doc = """重新汇总收件箱里的所有表格，生成一张新的总表。

    【什么时候才能用】
    只有在用户**明确要求**重新汇总、重跑一遍、重新算一次的时候才能调用。

    【什么时候绝对不能用】
    用户在**问数据**的时候（多少人、多少课时、谁的学生最多、某某是什么职称）
    **绝对不要调这个**——那些问题用 query_records 查一下就有答案了，
    重跑一遍又慢、又可能花钱、还会把他正在看的表换掉。

    【它会停下来等人点头】
    这个动作真的会动数据、真的可能花钱，所以调用之后系统会**暂停**，
    让用户看着你的理由决定同不同意。他不同意就不会执行。
    因此 `reason` 要写清楚你为什么要重跑——**用户会看到这句话再决定点不点**。
    一次只调一次，不要连着调好几个。"""

    @tool(RERUN_TOOL_NAME, description=doc)
    def rerun_merge(reason: str) -> str:
        return run_rerun(settings, store, sink, dry_run, reason)

    return rerun_merge


__all__ = [
    "MUTATING_TOOLS",
    "RERUN_TOOL_NAME",
    "ActionSink",
    "build_rerun_tool",
    "run_rerun",
]
