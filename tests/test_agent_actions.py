"""助手"动手"做的事：重跑汇总。

## 这个文件护着的是什么

``actions.py`` 是整个包里唯一会**改变东西**的文件。它有两个容易出事的点，
两个都属于"看起来一切正常"的那种：

1. **保险漏装。** 工具做出来了、模型调了、程序跑完了，只是那个"停下来
   问用户"的环节没有了。用户会以为程序问过他——其实没有。
   ``test_动手工具的名字和保险上登记的是同一个`` 钉死这一点。

2. **悄悄花钱。** 用户上次勾了"不花钱试跑"，这次让助手重跑，
   如果没沿用那个勾，点一下"照做"就花了钱，而卡片上可能还写着"不花钱"。
   ``test_重跑会沿用不花钱试跑那个勾`` 钉死这一点。

这两个都不是靠"我写的时候记得"能保证的，得有测试盯着。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wenhui.agent.actions import (
    MUTATING_TOOLS,
    RERUN_TOOL_NAME,
    ActionSink,
    build_rerun_tool,
    run_rerun,
)
from wenhui.agent.tools import QueryLedger
from wenhui.config import Settings

# --------------------------------------------------------------------------
# 假件
# --------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, n_records=3, n_issues=1):
        self.records = list(range(n_records))
        self.issues = list(range(n_issues))


@pytest.fixture()
def fake_pipeline(monkeypatch):
    """把 ``pipeline.run`` 换成一个假件，**不读文件、不调 AI、不花钱**。

    真的跑一遍汇总要读收件箱里的 Excel，还得建一张内存查询表——
    那些是 ``test_pipeline.py`` 的事。这里要验的是"重跑这个动作"
    本身：参数传对了没有、结果有没有交给界面、出了事怎么说。
    """
    import wenhui.pipeline as pipeline

    calls: list[dict] = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return _FakeResult()

    monkeypatch.setattr(pipeline, "run", fake_run)
    monkeypatch.setattr(pipeline, "find_template", lambda: None)
    return calls


# --------------------------------------------------------------------------
# 名字 —— 保险装没装全，全靠它
# --------------------------------------------------------------------------

def test_动手工具的名字和保险上登记的是同一个():
    """**这个文件里最值钱的一条。**

    ``chat.py`` 靠 ``RERUN_TOOL_NAME`` 去 ``interrupt_on`` 里登记保险。
    工具自己的名字要是和这个常量差一个字，保险就装到空气上了——
    工具照跑，一次都不问，而且**没有任何报错**。
    """
    sink = ActionSink()
    tool = build_rerun_tool(Settings(), store=None, sink=sink, dry_run=True)
    assert tool.name == RERUN_TOOL_NAME


def test_动手工具在会改数据的名单里():
    """名单里没有它的话，``chat.py`` 的断言就不会检查它——等于没装保险。"""
    assert RERUN_TOOL_NAME in MUTATING_TOOLS


# --------------------------------------------------------------------------
# 重跑这个动作本身
# --------------------------------------------------------------------------

def test_重跑成功时把新结果放进交接箱(fake_pipeline):
    """界面靠这个箱子拿到新结果。不放进去的话，用户点了"照做"、
    程序也确实重跑了，**但屏幕上的总表还是旧的**——他会以为没生效。"""
    sink = ActionSink()
    text = run_rerun(Settings(), store=None, sink=sink, dry_run=False, reason="用户要求")

    assert sink.ran is True
    assert sink.produced is True
    assert isinstance(sink.new_result, _FakeResult)
    assert sink.error == ""
    # 说给模型的必须是人话，而且要点出"新的总表已经更新到界面上了"
    assert "重新汇总完成" in text
    assert "3 条记录" in text


def test_重跑会沿用不花钱试跑那个勾(fake_pipeline):
    """**悄悄花钱那个坑的正解。**

    用户上次勾了"不花钱试跑"，这次让助手重跑——如果不把那个勾传下去，
    他点一下"好，照做"就真的调了 AI、真的花了钱，而他完全看不出来。
    """
    run_rerun(Settings(), store=None, sink=ActionSink(), dry_run=True, reason="")
    assert fake_pipeline[-1]["dry_run"] is True

    run_rerun(Settings(), store=None, sink=ActionSink(), dry_run=False, reason="")
    assert fake_pipeline[-1]["dry_run"] is False


def test_重跑是重新扫一遍收件箱(fake_pipeline):
    """用户说"重新汇总"的本意，多半是他刚往收件箱里补了几个单位的表。

    传死一个文件列表的话，新放进来的表永远不会被算进去——
    而他看到的是"重新汇总完成"，会以为算进去了。
    """
    run_rerun(Settings(), store=None, sink=ActionSink(), dry_run=True, reason="")
    assert fake_pipeline[-1]["files"] is None


def test_试跑时说明里要点出这次不准(fake_pipeline):
    """试跑用的是名称相似度，列名对得没那么准。

    不告诉模型的话，它会拿试跑的结果说得跟真的一样。
    """
    text = run_rerun(Settings(), store=None, sink=ActionSink(), dry_run=True, reason="")
    assert "试跑" in text


# --------------------------------------------------------------------------
# 出事的时候
# --------------------------------------------------------------------------

def test_收件箱空了不抛异常只说人话(monkeypatch):
    """**抛出去的话，用户点完"照做"看到一整页红色堆栈。**

    而他只是想让程序重跑一遍——收件箱空着是个他能自己修的毛病，
    应该说给他听，不是砸一个堆栈给他。
    """
    import wenhui.pipeline as pipeline

    def boom(**kwargs):
        raise pipeline.PipelineError("「收件箱」里没有找到表格文件。")

    monkeypatch.setattr(pipeline, "run", boom)
    monkeypatch.setattr(pipeline, "find_template", lambda: None)

    sink = ActionSink()
    text = run_rerun(Settings(), store=None, sink=sink, dry_run=True, reason="")

    assert sink.ran is True          # 跑了
    assert sink.produced is False    # 但没产出
    assert "收件箱" in sink.error
    assert "没" in text and "编" in text   # 明确叫模型别编一个结果出来


def test_意外错误也兜得住(monkeypatch):
    import wenhui.pipeline as pipeline

    def boom(**kwargs):
        raise RuntimeError("磁盘满了")

    monkeypatch.setattr(pipeline, "run", boom)
    monkeypatch.setattr(pipeline, "find_template", lambda: None)

    sink = ActionSink()
    text = run_rerun(Settings(), store=None, sink=sink, dry_run=True, reason="")
    assert "磁盘满了" in sink.error
    assert "磁盘满了" in text


def test_跑了但失败和压根没跑分得清():
    """``ran`` 和 ``produced`` 是两件事。

    合成一件的话，"跑了但失败"会被界面当成"没跑过"——
    用户点了"照做"，那件事没做成，屏幕上却什么都不说。
    """
    assert ActionSink().ran is False
    assert ActionSink().produced is False
    failed = ActionSink(ran=True, error="磁盘满了")
    assert failed.ran is True and failed.produced is False


# --------------------------------------------------------------------------
# 给模型看的说明书
# --------------------------------------------------------------------------

def test_说明书里写死了问数据时不许重跑():
    """**模型很爱"顺手刷新一下数据"。**

    而重跑一次又慢、又可能花钱、还会把用户正在看的表换掉。
    不写死这句话，用户问"乙老师多少课时"它都有可能去重跑。
    """
    tool = build_rerun_tool(Settings(), store=None, sink=ActionSink(), dry_run=True)
    doc = tool.description
    assert "绝对不要" in doc
    assert "query_records" in doc
    # 用户会看到它写的理由再决定点不点，这一条得让它知道
    assert "reason" in doc
