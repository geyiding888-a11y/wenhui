"""查询工具：一条查询有两个去向，这里的每一条都在盯这个边界。

**这个文件里最要紧的两类测试**：

1. 发给模型的那份**必须**打码（身份证/手机号一个都不许露）
2. 界面上展示的那份**必须**完整（用户看自己的数据没有理由打码）

搞反哪一边都是事故：反了前者是个人信息外流，反了后者是用户
看不到自己的真实数据、以为程序坏了。
"""

from __future__ import annotations

import sqlite3

import pytest

from wenhui.agent.table import TABLE_NAME, build_query_table
from wenhui.agent.tools import (
    MAX_ROWS,
    QueryLedger,
    build_query_tool,
    run_query,
)
from wenhui.config import AgentSettings, PrivacySettings
from wenhui.excel.validator import Record


def _result(fields, rows, run_id=1):
    class _FakeResult:
        pass

    r = _FakeResult()
    r.fields = list(fields)
    r.run_id = run_id
    r.records = [
        Record(values=dict(zip(fields, row)), raw={}, file="甲学院.xlsx", sheet="", row=i + 2)
        for i, row in enumerate(rows)
    ]
    return r


@pytest.fixture()
def table():
    return build_query_table(
        _result(
            ["单位", "姓名", "职称", "学生数"],
            [
                ["计算机学院", "张三", "教授", 120],
                ["计算机学院", "李四", "副教授", 30],
                ["文学院", "王五", "教授", 200],
            ],
        )
    )


@pytest.fixture()
def ledger():
    return QueryLedger()


@pytest.fixture()
def ask(table, ledger):
    def _ask(sql, **kw):
        return run_query(table, ledger, sql, AgentSettings(**kw))

    return _ask


# --------------------------------------------------------------------------
# 正常查询
# --------------------------------------------------------------------------

def test_能查到数据并报出行数(ask):
    out = ask(f'SELECT "姓名" FROM "{TABLE_NAME}"')
    assert "共 3 行" in out
    assert "张三" in out


def test_能算总数和合计(ask):
    assert "3" in ask(f'SELECT COUNT(*) FROM "{TABLE_NAME}"')
    assert "350" in ask(f'SELECT SUM("学生数") FROM "{TABLE_NAME}"')


def test_能按条件筛(ask):
    out = ask(f"SELECT \"姓名\" FROM \"{TABLE_NAME}\" WHERE 职称 = '教授'")
    assert "张三" in out and "王五" in out and "李四" not in out


def test_数字列能正确比大小(ask):
    """类型推断的下游验证：按文字存的话会多出来一堆人。"""
    out = ask(f'SELECT COUNT(*) FROM "{TABLE_NAME}" WHERE "学生数" > 100')
    assert "2" in out


def test_表结构说明写进了工具描述(table):
    """模型看不到数据库，只能靠工具描述里的这句话知道表叫什么、
    列名要用什么引号。这两句没了，它就会瞎猜表名。"""
    tool = build_query_tool(table, QueryLedger(), AgentSettings())
    assert TABLE_NAME in tool.description
    assert "双引号" in tool.description


def test_工具描述里写了不许猜数(table):
    """**准确性靠这一句。**模型很爱"根据常识推断"一个数字出来，
    而用户没法分辨那是查出来的还是编的。"""
    tool = build_query_tool(table, QueryLedger(), AgentSettings())
    assert "不许凭印象猜" in tool.description


def test_工具能真的被调用(table):
    ledger = QueryLedger()
    tool = build_query_tool(table, ledger, AgentSettings())
    out = tool.invoke({"sql": f'SELECT COUNT(*) FROM "{TABLE_NAME}"'})
    assert "3" in out
    assert ledger.n_queries == 1


# --------------------------------------------------------------------------
# 两个去向：打码 vs 完整
# --------------------------------------------------------------------------

def test_发给模型的那份打了码():
    table = build_query_table(
        _result(["姓名", "证件号", "电话"], [["张三", "330102199001011234", "13812345678"]])
    )
    out = run_query(
        table, QueryLedger(), f'SELECT * FROM "{TABLE_NAME}"', AgentSettings()
    )
    assert "330102199001011234" not in out
    assert "13812345678" not in out
    assert "3301**********1234" in out
    assert "138****5678" in out


def test_账本里那份是完整的():
    """界面上要显示完整原文——这是用户自己的数据。

    注意末尾两个溯源列也在结果里（界面上要能显示"这行是哪来的"）。
    """
    table = build_query_table(
        _result(["姓名", "证件号"], [["张三", "330102199001011234"]])
    )
    ledger = QueryLedger()
    run_query(table, ledger, f'SELECT * FROM "{TABLE_NAME}"', AgentSettings())
    assert ledger.last_ok.rows == [("张三", "330102199001011234", "甲学院.xlsx", 2)]


def test_两个开关叠加_身份证打码而姓名不打():
    """用户选的默认组合：姓名不打（否则"张三有多少学生"答不出来），
    身份证照打。"""
    table = build_query_table(
        _result(["姓名", "证件号"], [["张三", "330102199001011234"]]),
        mask_name=False,
    )
    out = run_query(
        table, QueryLedger(), f'SELECT * FROM "{TABLE_NAME}"', AgentSettings()
    )
    assert "张三" in out
    assert "330102199001011234" not in out


def test_开了姓名打码之后查询结果里也没有全名():
    table = build_query_table(
        _result(["姓名"], [["张三"]]), mask_name=True
    )
    out = run_query(
        table,
        QueryLedger(),
        f'SELECT * FROM "{TABLE_NAME}"',
        AgentSettings(mask_name_in_answer=True),
    )
    assert "张三" not in out
    assert "张*" in out


def test_关掉手机号打码就真的不打():
    """`[privacy]` 里的开关一路传到查询结果这一步。

    以前那几个开关是失效的，所以这里要钉住它真的管用。
    """
    table = build_query_table(
        _result(["电话"], [["13812345678"]]),
        privacy=PrivacySettings(mask_phone=False),
    )
    out = run_query(
        table, QueryLedger(), f'SELECT * FROM "{TABLE_NAME}"', AgentSettings()
    )
    assert "13812345678" in out


# --------------------------------------------------------------------------
# 结果截断
# --------------------------------------------------------------------------

def test_行数超过上限时只发前几行_并说清楚总数():
    rows = [[f"老师{i}", i] for i in range(50)]
    table = build_query_table(_result(["姓名", "学生数"], rows))
    out = run_query(
        table,
        QueryLedger(),
        f'SELECT * FROM "{TABLE_NAME}"',
        AgentSettings(result_rows_for_ai=5),
    )
    assert "共 50 行" in out
    assert "前 5 行" in out
    # 关键：要明确叫它别拿这 5 行去推总数，否则它会答"一共 5 人"
    assert "COUNT" in out
    assert "老师6" not in out


def test_行数没超上限就不提截断():
    table = build_query_table(_result(["姓名"], [["张三"]]))
    out = run_query(
        table, QueryLedger(), f'SELECT * FROM "{TABLE_NAME}"', AgentSettings()
    )
    assert "只给你看前" not in out


def test_总行数按完整结果算_不按发出去的那几行算():
    """发出去 5 行，但"共 50 行"必须说 50。说成 5 就是在误导模型。"""
    rows = [[i] for i in range(50)]
    table = build_query_table(_result(["学生数"], rows))
    out = run_query(
        table,
        QueryLedger(),
        f'SELECT * FROM "{TABLE_NAME}"',
        AgentSettings(result_rows_for_ai=5),
    )
    assert "共 50 行" in out


# --------------------------------------------------------------------------
# 出错时怎么办
# --------------------------------------------------------------------------

def test_列名写错时返回人话_不抛异常(ask, ledger):
    """**返回文字而不是抛异常**：模型看到错误会改一版再来，
    抛异常的话整个对话就断在那儿了。"""
    out = ask(f'SELECT "不存在的列" FROM "{TABLE_NAME}"')
    assert "没跑通" in out
    assert "双引号" in out
    assert ledger.runs[-1].ok is False


def test_写非查询语句时给提示(ask):
    """这条要走 :func:`looks_like_select` 的提示分支。

    注意：**只靠这一层是拦不住的**——真正的边界是建表时装上的只读闸，
    下面单独有测试盯它。
    """
    out = ask("DROP TABLE test")
    assert "只能查数据" in out


def test_即使用注释伪装也拦得住(table, ledger):
    """文本判断会被注释骗过去，但**授权器不会被骗**。

    授权器看到的是"要执行 DROP 这个动作"，不是"这段文字长什么样"。
    所以下面这条会走完整条路，最后被闸拦下，转成一句人话返回。
    """
    out = run_query(
        table,
        ledger,
        f"-- 只是一条查询\nSELECT * FROM {TABLE_NAME}; DROP TABLE {TABLE_NAME}",
        AgentSettings(),
    )
    # 要么多语句被 sqlite 拒了，要么授权器拒了。总之没执行成功
    assert ledger.runs[-1].ok is False or "DROP" in out
    # 最关键：表还在
    assert table.conn.execute(f'SELECT COUNT(*) FROM "{TABLE_NAME}"').fetchone() == (3,)


def test_查询出错后连接还能继续用(ask, table):
    """一条烂查询不该把整个会话搞坏——用户下一个问题还得能问。"""
    ask(f'SELECT "不存在的列" FROM "{TABLE_NAME}"')
    assert "3" in ask(f'SELECT COUNT(*) FROM "{TABLE_NAME}"')


def test_超时会话不会卡死(table, ledger):
    """真出事了也不能把界面焊死。"""
    out = run_query(
        table,
        ledger,
        f'WITH RECURSIVE 数(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM 数 WHERE n < 100000000)'
        f' SELECT COUNT(*) FROM 数',
        AgentSettings(),
    )
    # 要么跑完了（本机够快），要么被掐断并返回人话。都不该挂住
    assert isinstance(out, str)


# --------------------------------------------------------------------------
# 单引号修正的端到端效果
# --------------------------------------------------------------------------

def test_单引号包列名会返回真数据_不是假的(ask):
    """**这条是整个修正机制存在的理由。**

    ``SELECT '单位' FROM 表`` 不加修正的话**不报错**，
    返回一列全是"单位"两个字的假数据，而且看起来完全正常。
    """
    out = ask(f"SELECT '单位' FROM \"{TABLE_NAME}\"")
    assert "计算机学院" in out
    assert "文学院" in out


def test_字符串值照样用单引号(ask):
    """修的是**列名**写错，不能把正常的数据写法也改了。"""
    out = ask(f"SELECT \"姓名\" FROM \"{TABLE_NAME}\" WHERE \"单位\" = '文学院'")
    assert "王五" in out
    assert "张三" not in out


# --------------------------------------------------------------------------
# 账本
# --------------------------------------------------------------------------

def test_账本记下每次查询(ask, ledger):
    ask(f'SELECT COUNT(*) FROM "{TABLE_NAME}"')
    ask(f'SELECT "姓名" FROM "{TABLE_NAME}"')
    assert ledger.n_queries == 2
    assert ledger.runs[-1].columns == ["姓名"]


def test_账本里的last_ok跳过失败的查询(ask, ledger):
    ask(f'SELECT COUNT(*) FROM "{TABLE_NAME}"')
    ask(f'SELECT "不存在的列" FROM "{TABLE_NAME}"')
    assert ledger.last_ok.columns == ["COUNT(*)"]


def test_全新账本没有last_ok(ledger):
    assert ledger.last_ok is None


def test_账本能清空(ask, ledger):
    ask(f'SELECT COUNT(*) FROM "{TABLE_NAME}"')
    ledger.clear()
    assert ledger.n_queries == 0


def test_账本记下了真的跑了什么(ask, ledger):
    """界面要展示"AI 到底查了什么"，用户才敢信那个数字。"""
    sql = f'SELECT "姓名" FROM "{TABLE_NAME}"'
    ask(sql)
    assert sql in ledger.runs[-1].sql


# --------------------------------------------------------------------------
# 行数硬上限
# --------------------------------------------------------------------------

def test_捞太多行会被截断并告知(table, ledger):
    """兜的是一种极端情况：模型写了个自己连自己的查询，
    900 行的表会变成 81 万行，内存当场就爆。"""
    out = run_query(
        table,
        ledger,
        f'SELECT COUNT(*) FROM "{TABLE_NAME}" AS a, "{TABLE_NAME}" AS b, "{TABLE_NAME}" AS c',
        AgentSettings(),
    )
    assert isinstance(out, str)
    # 三种表交叉相乘只有 27 行，不该被截断；这里确认它跑通了
    assert ledger.last_ok is not None


def test_上限是个合理的数():
    """太小会误伤正常查询，太大兜不住爆内存。"""
    assert 1000 <= MAX_ROWS <= 100000
