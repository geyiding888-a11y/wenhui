"""装配层：把模型、工具、记忆拼成一个能问话的助手。

## 这个文件里最值钱的一条

``test_整个装配能跑通_工具真的执行了`` —— 它用一个**假模型**驱动完整的
LangGraph 循环：模型要先说"我要调工具"，工具在**工作线程**里真的执行，
结果回给模型，模型再说人话。

这一步之所以值钱，是因为它能零成本验到三个平时要花钱才能发现的问题：

1. ``check_same_thread=False`` 到底有没有生效（没生效的话工具全报错，
   而表现是"模型答不出来"，看着像 AI 笨）
2. ``create_agent`` 的参数拼得对不对（拼错了要到真提问那一刻才知道）
3. 记账回调到底有没有被触发（不触发的话花费一直显示 0）

真机跑一遍这三点要花几分钱、还要手动核对。这里跑一遍不要钱。
"""

from __future__ import annotations

import pytest

from wenhui.agent.actions import RERUN_TOOL_NAME, ActionSink
from wenhui.agent.chat import (
    NO_GUESSING_RULE,
    AskOutcome,
    _is_ai_text,
    _make_usage_recorder,
    _text_of,
    ask,
    build_system_prompt,
    new_memory,
    resume,
)
from wenhui.agent.table import TABLE_NAME, build_query_table
from wenhui.agent.tools import QueryLedger
from wenhui.config import AgentSettings, LLMSettings, Settings
from wenhui.excel.validator import Record
from wenhui.llm import BudgetExceeded, LLMClient

FAKE_USAGE = {"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000}


# --------------------------------------------------------------------------
# 假件
# --------------------------------------------------------------------------


def _table(rows=3):
    class _FakeResult:
        pass

    r = _FakeResult()
    r.fields = ["单位", "姓名", "学生数"]
    r.run_id = 1
    r.records = [
        Record(
            values={"单位": "计算机学院", "姓名": f"老师{i}", "学生数": i * 10},
            raw={}, file="甲学院.xlsx", sheet="", row=i + 2,
        )
        for i in range(1, rows + 1)
    ]
    return build_query_table(r)


def _ai(content="", tool_calls=None):
    from langchain_core.messages import AIMessage

    return AIMessage(
        content=content,
        tool_calls=tool_calls or [],
        usage_metadata=dict(FAKE_USAGE),
    )


def _scripted_model(responses):
    """一个按剧本回话的假模型。**一次网络都不发。**

    ``bind_tools`` 是必须实现的：``create_agent`` 建 agent 时会调它，
    ``BaseChatModel`` 的默认实现是直接抛 NotImplementedError。
    """
    from langchain_core.language_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult

    class _Scripted(BaseChatModel):
        responses: list
        cursor: int = 0

        @property
        def _llm_type(self) -> str:
            return "scripted-for-test"

        def bind_tools(self, tools, **kwargs):  # noqa: ANN001
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
            index = min(self.cursor, len(self.responses) - 1)
            self.cursor += 1
            message = self.responses[index]
            return ChatResult(generations=[ChatGeneration(message=message)])

    return _Scripted(responses=responses)


@pytest.fixture()
def settings():
    return Settings()


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------

def test_提示词要求说中文(settings):
    """**不加这条它会用英文答。**实测过。"""
    text = build_system_prompt(_table(), settings)
    assert "简体中文" in text


def test_提示词写死了不许猜(settings):
    """准确性的命根子。这句话没了，它会开始"根据常识推断"数字。"""
    text = build_system_prompt(_table(), settings)
    assert NO_GUESSING_RULE in text


def test_提示词里带着表结构(settings):
    """模型看不到数据库，表名和列名只能从提示词里知道。"""
    text = build_system_prompt(_table(), settings)
    assert TABLE_NAME in text
    assert "学生数" in text
    # 写查询时列名要用双引号，这条得告诉它
    assert "双引号" in text


def test_没有数据时提示词让它别编(settings):
    """**没数据比有数据更危险**：它更容易顺着常识编一个出来。

    提示词要明确叫它说"先去汇总"，而不是硬写一条 SQL 然后编答案。
    """
    text = build_system_prompt(_table(rows=0), settings)
    assert "一行数据都没有" in text
    assert "不要编造" in text


def test_有数据时提示词报出总行数(settings):
    assert "3 行数据" in build_system_prompt(_table(), settings)


def test_提示词里的样本是打过码的(settings):
    """表结构说明**每次提问都会发出去**，比查询结果发得还频繁。
    这里漏一个号码，泄露就是持续性的。"""
    class _FakeResult:
        pass

    r = _FakeResult()
    r.fields = ["姓名", "证件号"]
    r.run_id = 1
    r.records = [
        Record(values={"姓名": "张三", "证件号": "330102199001011234"},
               raw={}, file="a.xlsx", sheet="", row=2)
    ]
    text = build_system_prompt(build_query_table(r), settings)
    assert "330102199001011234" not in text
    assert "3301**********1234" in text


# --------------------------------------------------------------------------
# 记账回调 —— 这是"花费显示 0"那个 bug 的正解
# --------------------------------------------------------------------------

class _FakeGeneration:
    def __init__(self, message):
        self.message = message


class _FakeLLMResult:
    def __init__(self, *messages):
        self.generations = [[_FakeGeneration(m)] for m in messages]


def test_回调按真实调用次数记账():
    client = LLMClient(LLMSettings(), "sk-fake", cost_limit_cny=5.0)
    recorder = _make_usage_recorder(client)
    recorder.on_llm_end(_FakeLLMResult(_ai(content="一"), _ai(content="二")))
    assert client.usage.calls == 2
    assert recorder.recorded == 2
    assert client.cost_so_far() > 0


def test_回调记不上账时能看出来():
    """没有用量信息的消息，``recorded`` 不该往上加。

    **这条防止"花费显示 0"换个形式回来**：如果记不上也返回成功，
    界面就没法发现费用被低估了。
    """
    from langchain_core.messages import AIMessage

    client = LLMClient(LLMSettings(), "sk-fake")
    recorder = _make_usage_recorder(client)
    bare = AIMessage(content="没有用量信息")
    recorder.on_llm_end(_FakeLLMResult(bare))
    assert recorder.recorded == 0
    assert client.usage.calls == 1     # 次数还是记了——"调过"和"没调过"不一样


def test_回调把失败也记上():
    client = LLMClient(LLMSettings(), "sk-fake")
    recorder = _make_usage_recorder(client)
    recorder.on_llm_error(RuntimeError("网络断了"))
    assert client.usage.failures == 1


def test_一次调用返回多个候选都记():
    """n>1 的时候一次调用有好几条消息，漏记就是少算钱。"""
    client = LLMClient(LLMSettings(), "sk-fake")
    recorder = _make_usage_recorder(client)
    recorder.on_llm_end(_FakeLLMResult(_ai(content="a"), _ai(content="b"), _ai(content="c")))
    assert client.usage.calls == 3


# --------------------------------------------------------------------------
# 从返回值里取答案
# --------------------------------------------------------------------------

def test_认出助手的文字消息():
    assert _is_ai_text(_ai(content="有答案"))
    assert not _is_ai_text(_ai(content=""))            # 空的不算
    assert not _is_ai_text(object())                    # 不是消息的不算


def test_工具调用请求不算答案():
    """模型说"我要调工具"的那一刻还没有答案。

    直接取"最后一条助手消息"的话，正好会取到这条没有文字的，
    用户看到的就是一片空白。
    """
    from langchain_core.messages import AIMessage

    calling = AIMessage(
        content="",
        tool_calls=[{"name": "query_records", "args": {"sql": "SELECT 1"}, "id": "c1", "type": "tool_call"}],
    )
    assert not _is_ai_text(calling)


def test_内容被拆成好几块也能取出来():
    """有的模型把回答拆成 [{type: text}, ...] 这种结构。"""
    from langchain_core.messages import AIMessage

    message = AIMessage(content=[{"type": "text", "text": "第一段"},
                                 {"type": "text", "text": "第二段"}])
    assert _text_of(message) == "第一段\n第二段"


def test_取文字时两边空白会去掉():
    from langchain_core.messages import AIMessage

    assert _text_of(AIMessage(content="  答案  ")) == "答案"


# --------------------------------------------------------------------------
# 没记上账要能看见
# --------------------------------------------------------------------------

def test_答出来了但没记账要报警():
    out = AskOutcome(answer="一共 3 人", n_recorded=0)
    assert out.usage_uncounted is True


def test_正常记上账就不报警():
    assert AskOutcome(answer="一共 3 人", n_recorded=2).usage_uncounted is False


def test_没答案时不报警():
    """出错本来就没有花费，不该再叠一句"费用可能被低估"。"""
    assert AskOutcome(error="连不上").usage_uncounted is False


# --------------------------------------------------------------------------
# 整个装配跑一遍（零成本）
# --------------------------------------------------------------------------

def test_整个装配能跑通_工具真的执行了(settings):
    """**这个文件里最值钱的一条。**

    剧本：模型先说"我要查一下"，工具在**工作线程**里真的执行，
    结果回给模型，模型再说人话。

    一次网络都不发，但把下面这些全验到了：
    - ``create_agent`` 的参数拼对了没有
    - ``check_same_thread=False`` 生效了没有（没生效这里就炸）
    - 工具执行完有没有进账本
    - 记账回调有没有被触发
    - 最后取到的答案是"人话"而不是"工具调用请求"
    """
    import wenhui.agent.chat as chat

    table = _table()
    ledger = QueryLedger()
    memory = new_memory()
    client = LLMClient(LLMSettings(), "sk-fake", cost_limit_cny=5.0)

    model = _scripted_model([
        _ai(tool_calls=[{
            "name": "query_records",
            "args": {"sql": f'SELECT COUNT(*) FROM "{TABLE_NAME}"'},
            "id": "call-1",
            "type": "tool_call",
        }]),
        _ai(content="一共 3 人。"),
    ])
    real_build = chat.build_agent
    chat.build_agent = lambda *a, **kw: __import__("langchain.agents", fromlist=["create_agent"]).create_agent(
        model, [__import__("wenhui.agent.tools", fromlist=["build_query_tool"]).build_query_tool(table, ledger, settings.agent)],
        system_prompt="测试", checkpointer=memory,
    )
    try:
        outcome = ask(settings, client, table, "一共有多少人？", memory, ledger)
    finally:
        chat.build_agent = real_build

    assert outcome.error == "", outcome.error
    assert outcome.answer == "一共 3 人。"
    # 工具真的跑了，而且结果进了账本
    assert ledger.n_queries == 1
    assert ledger.last_ok.ok
    assert "3" in str(ledger.last_ok.rows)
    # 记账真的发生了（修之前这里是 0）
    assert outcome.n_recorded >= 1
    assert client.cost_so_far() > 0


def test_第二轮能记得第一轮问过什么(settings):
    """对话记忆。**没有它，追问"那李四呢"它就不知道你在说谁。**"""
    import wenhui.agent.chat as chat

    table = _table()
    client = LLMClient(LLMSettings(), "sk-fake")
    memory = new_memory()
    seen: list[list] = []

    def fake_build(*args, **kwargs):
        class _Agent:
            def invoke(self, payload, config):
                seen.append(list(payload["messages"]))
                return {"messages": [_ai(content=f"第 {len(seen)} 次回答")]}

        return _Agent()

    real_build = chat.build_agent
    chat.build_agent = fake_build
    try:
        ask(settings, client, table, "第一个问题", memory)
        ask(settings, client, table, "第二个问题", memory)
    finally:
        chat.build_agent = real_build

    assert len(seen) == 2
    # 第二次调用时只发了新问题——记忆是存在 checkpointer 里的，不是靠重发历史
    assert seen[1][0]["content"] == "第二个问题"


# --------------------------------------------------------------------------
# 出错的时候
# --------------------------------------------------------------------------

def test_空问题不浪费一次调用(settings):
    client = LLMClient(LLMSettings(), "sk-fake")
    out = ask(settings, client, _table(), "   ", new_memory())
    assert out.error
    assert client.usage.calls == 0


def test_超预算时直接停下_不去调模型(settings):
    """用户设的上限必须真的管用，而问数助手是绕过 ``structured()`` 的。"""
    client = LLMClient(LLMSettings(), "sk-fake", cost_limit_cny=0.000001)
    client.record_usage(FAKE_USAGE)     # 先花掉一点，超过上限
    out = ask(settings, client, _table(), "一共多少人", new_memory())
    assert "上限" in out.error
    assert client.usage.calls == 1      # 没有再多调一次


def test_模型抛异常时转成人话不往上炸(settings):
    """**界面层兜底**：抛出去就是白屏，用户以为程序坏了。"""
    import wenhui.agent.chat as chat

    def boom(*args, **kwargs):
        raise RuntimeError("模型服务 500")

    real_build = chat.build_agent
    chat.build_agent = boom
    try:
        out = ask(settings, LLMClient(LLMSettings(), "sk-fake"), _table(), "问一句", new_memory())
    finally:
        chat.build_agent = real_build

    assert "这次没问成" in out.error
    assert "500" in out.error


def test_模型什么都没答时说清楚(settings):
    """一轮下来一条文字都没有（比如次数用完了），不能给用户一片空白。"""
    import wenhui.agent.chat as chat

    def empty(*args, **kwargs):
        class _Agent:
            def invoke(self, payload, config):
                return {"messages": [_ai(content="")]}

        return _Agent()

    real_build = chat.build_agent
    chat.build_agent = empty
    try:
        out = ask(settings, LLMClient(LLMSettings(), "sk-fake"), _table(), "问一句", new_memory())
    finally:
        chat.build_agent = real_build

    assert out.error
    assert "没有给出回答" in out.error


# --------------------------------------------------------------------------
# "动手先问" —— 停下来等人点头
# --------------------------------------------------------------------------
#
# 下面这几条用的都是**真的** build_agent（不 monkeypatch），所以它们验的是
# 整个中断/恢复回路真的接对了：中间件装上了没有、中断内容取出来了没有、
# 用户点完能不能接着往下走。
#
# 特别注意 test_点同意它才真的跑 里悄悄发生的一件事：``resume`` 内部**又造了一个
# 全新的 agent**（Streamlit 每次重画都会把一切重建），它能接着上次的中断走，
# 靠的是两次共用同一份 memory。这一条不成立的话，用户点完按钮就会从头再来。


def _client_with(model, **kwargs) -> LLMClient:
    """造一个客户端，但把里面的模型换成假的。**一次网络都不发。**"""
    client = LLMClient(LLMSettings(), "sk-fake", **kwargs)
    client._model = model          # 绕过懒加载，直接塞进去
    return client


def _rerun_call(reason="用户要求重新汇总"):
    return _ai(tool_calls=[{
        "name": RERUN_TOOL_NAME,
        "args": {"reason": reason},
        "id": "call-rerun",
        "type": "tool_call",
    }])


def _actions(tmp_path, dry_run=True):
    from wenhui.agent.chat import ActionContext
    from wenhui.store import Store

    sink = ActionSink()
    ctx = ActionContext(sink=sink, store=Store(tmp_path / "t.db"), dry_run=dry_run)
    return ctx, sink


@pytest.fixture()
def no_real_merge(monkeypatch):
    """重跑汇总不真的去读收件箱。**读文件、建表那些是 test_pipeline 的事。**"""
    import wenhui.pipeline as pipeline

    class _Result:
        records = ["甲", "乙", "丙"]
        issues = ["缺了个日期"]

    monkeypatch.setattr(pipeline, "run", lambda **kw: _Result())
    monkeypatch.setattr(pipeline, "find_template", lambda: None)


def test_说重新汇总时会停下来问(settings, tmp_path):
    """**"动手先问"的核心：它停住了，而且一次都没动。**

    如果只是"停下来"但工具已经跑了，那这个保险是假的——
    用户点的那个"同意"变成了一个事后通知。
    """
    ctx, sink = _actions(tmp_path)
    client = _client_with(_scripted_model([_rerun_call(), _ai(content="已经重新汇总好了。")]))

    out = ask(settings, client, _table(), "重新汇总一下", new_memory(),
              QueryLedger(), actions=ctx)

    assert out.waiting, "它没有停下来问"
    assert out.error == ""            # ★ 不能报成"AI 没有给出回答"
    assert out.answer == ""           # 还没做事，当然没有答案
    assert sink.ran is False          # ★ 工具一次都没跑
    assert out.pending[0].name == RERUN_TOOL_NAME
    # 用户要靠这个判断"它到底想干什么"，取不到的话界面就只能干说一句"它想做事"
    assert "重新汇总" in out.pending[0].args["reason"]
    assert out.usage_uncounted is False   # 别误报"费用被低估"


def test_点同意它才真的跑(settings, tmp_path, no_real_merge):
    """点了「好，照做」→ 工具真的执行，新结果交回界面。"""
    ctx, sink = _actions(tmp_path)
    memory = new_memory()
    client = _client_with(_scripted_model([
        _rerun_call(),
        _ai(content="好的，已经重新汇总完了，一共 3 条记录。"),
    ]))

    ask(settings, client, _table(), "重新汇总一下", memory, QueryLedger(), actions=ctx)
    assert sink.ran is False

    out = resume(settings, client, _table(), memory,
                 [{"type": "approve"}], QueryLedger(), actions=ctx)

    assert sink.ran is True, "点了同意，工具却没跑"
    assert sink.produced, "跑了但新结果没交回界面——屏幕上还是旧表"
    assert "重新汇总完了" in out.answer
    assert not out.waiting


def test_点先别动它一次都不跑(settings, tmp_path, no_real_merge):
    """点了「先别动」→ 工具一次都不执行，助手接着说人话。

    **这条比"点同意"更重要**：拒绝之后要是它还是跑了，
    用户就再也不会相信那两个按钮了。
    """
    ctx, sink = _actions(tmp_path)
    memory = new_memory()
    client = _client_with(_scripted_model([
        _rerun_call(),
        _ai(content="好的，那就不动了。"),
    ]))

    ask(settings, client, _table(), "重新汇总一下", memory, QueryLedger(), actions=ctx)
    out = resume(settings, client, _table(), memory,
                 [{"type": "reject", "message": "先别动，我只是问问"}],
                 QueryLedger(), actions=ctx)

    assert sink.ran is False, "用户说了先别动，工具还是跑了"
    assert sink.produced is False
    assert out.answer, "拒绝了之后助手应该接着说句话，不能一片空白"
    assert not out.waiting


def test_答复条数对不上时不往用户脸上扔堆栈(settings, tmp_path, no_real_merge):
    """框架对条数是**硬校验**的（实测会抛 ``ValueError``）。

    真抛出来的话，用户看到一整页红色堆栈，而他只是点了个按钮。
    界面那边是"按待批动作的个数生成答复"，本来就对得上；
    这一条是万一哪天改坏了，兜住别炸。
    """
    ctx, _ = _actions(tmp_path)
    memory = new_memory()
    client = _client_with(_scripted_model([_rerun_call(), _ai(content="好")]))
    ask(settings, client, _table(), "重新汇总一下", memory, QueryLedger(), actions=ctx)

    out = resume(settings, client, _table(), memory, [], QueryLedger(), actions=ctx)
    assert out.error
    assert not out.waiting


def test_上一件事没答复时不会被默默吞掉(settings, tmp_path, no_real_merge):
    """**真机上验出来的一个坑。**

    中断还挂着的时候再问一句，框架**不报错**——它把消息收进状态、
    然后原地又回到那个中断上。用户看到的是"我问了它，它不理我"，
    而且没有任何东西能告诉他为什么。

    界面那边有栏杆挡着（有待批动作时不给输入框）。这一条验的是栏杆万一
    被拆了，装配层也得把话说明白，而不是把人晾在那儿。
    """
    ctx, _ = _actions(tmp_path)
    memory = new_memory()
    client = _client_with(_scripted_model([_rerun_call(), _ai(content="好")]))

    ask(settings, client, _table(), "重新汇总一下", memory, QueryLedger(), actions=ctx)

    out = ask(settings, client, _table(), "一共有多少人？", memory,
              QueryLedger(), actions=ctx)

    assert out.error, "新问题被吞了——用户会以为它坏了"
    assert "答复" in out.error
    # 不要在这儿再画一张卡片：上一张还在对话记录里挂着，画两张用户会看晕
    assert not out.waiting


def test_没有动手能力时上面那道检查不误伤(settings):
    """不传 ``actions`` 的助手压根不会停下来等人。

    这时候要是把"检查有没有晾着的动作"也做了，等于每问一句都去翻一次
    记忆——翻出点什么就误报，把正常提问堵死。
    """
    client = _client_with(_scripted_model([_ai(content="一共 3 人。")]))
    out = ask(settings, client, _table(), "一共多少人", new_memory())
    assert out.error == ""
    assert out.answer == "一共 3 人。"


def test_不给动手能力时根本造不出动手工具(settings, monkeypatch):
    """不传 ``actions`` 的助手**只会查、不会动**——工具压根不存在，
    模型想调也调不到。这是个有用的降级：不想让它动手就别传。"""
    import wenhui.agent.chat as chat

    built: list = []
    monkeypatch.setattr(chat, "build_rerun_tool", lambda *a, **kw: built.append(1))
    client = _client_with(_scripted_model([_ai(content="好")]))
    chat.build_agent(settings, client, _table(), QueryLedger(), new_memory())
    assert built == []


def test_给了动手能力就会把它造出来并装上保险(settings, monkeypatch, tmp_path):
    import wenhui.agent.chat as chat

    built: list = []
    # 换成一个**真的工具对象**：假件必须长着 ``.name``，
    # 因为紧接着那行断言就是靠它去核对保险装没装上的。
    monkeypatch.setattr(
        chat, "build_rerun_tool", lambda *a, **kw: built.append(1) or _stub_tool()
    )
    client = _client_with(_scripted_model([_ai(content="好")]))
    ctx, _ = _actions(tmp_path)
    chat.build_agent(settings, client, _table(), QueryLedger(), new_memory(), ctx)
    assert built == [1]


def _stub_tool():
    from langchain_core.tools import tool

    @tool(RERUN_TOOL_NAME, description="假的，只为了有名字和参数")
    def stub(reason: str) -> str:
        return "假的"

    return stub


def test_忘了装保险会被当场拦住(settings):
    """**"先装保险，再给枪上子弹"的机械化版本。**

    漏装的后果是静默的：工具照跑，只是不再问用户。所以这里必须**当场**炸，
    而不是等某天它不问自取地把用户的表重跑了一遍。
    """
    from wenhui.agent import chat

    with pytest.raises(RuntimeError, match="停下来问用户"):
        chat._assert_all_actions_gated([RERUN_TOOL_NAME], {})

    # 只读的工具不需要保险——它改不坏任何东西，拦它只会让用户每问一句点一次同意
    chat._assert_all_actions_gated(["query_records"], {})


def test_中断内容缺胳膊少腿也认得出来(settings):
    """这段跑在"模型已经返回、界面还没画"之间，抛异常的话用户看到的是白屏。"""
    from wenhui.agent.chat import _pending_actions

    assert _pending_actions({}) == []
    assert _pending_actions({"__interrupt__": []}) == []
    assert _pending_actions({"__interrupt__": [object()]}) == []
    assert _pending_actions({"__interrupt__": [{"value": {"action_requests": [None]}}]}) == []
    # args 不是字典（框架换版本了）也不能炸
    got = _pending_actions({
        "__interrupt__": [{"value": {"action_requests": [{"name": "x", "args": "乱码"}]}}]
    })
    assert got[0].name == "x" and got[0].args == {}


# --------------------------------------------------------------------------
# 关掉这个功能
# --------------------------------------------------------------------------

def test_关掉之后配置读得出来():
    assert AgentSettings(enabled=False).enabled is False
