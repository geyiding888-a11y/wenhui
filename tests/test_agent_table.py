"""建查询表：把汇总结果变成一张能写 SQL 查的表。

这里盯的是三类事：

1. **列名对不对得上** —— 你从汇总表里看到的列，和它查得到的列，
   必须是同一批名字。对不上就会出现"我说'来源文件'它说没有这一列"。
2. **类型推断对不对** —— 数字列按文字存的话，``学生数 > 100``
   会**静默算错**（按文字比大小，"9" 比 "100" 大）。
3. **发出去的样本一定打过码** —— 这是唯一的脱敏出口，漏一个就是持续泄露。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

import pytest

from wenhui.agent.table import TABLE_NAME, build_query_table, mask_name, mask_samples
from wenhui.config import PrivacySettings
from wenhui.excel.validator import Record


def _result(fields, rows, run_id=1):
    """造一个"假的汇总结果"，形状和真的 PipelineResult 一样。

    只用到 fields / records / run_id 三样，所以这里用一个轻量对象，
    不用真的去跑一遍汇总。
    """

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


# --------------------------------------------------------------------------
# 建表
# --------------------------------------------------------------------------

def test_列名和汇总表一致_并且带溯源列():
    table = build_query_table(_result(["姓名", "课时"], [["张三", 128]]))
    assert table.columns == ["姓名", "课时", "来源文件", "来源行"]


def test_行数对得上():
    table = build_query_table(_result(["姓名"], [["张三"], ["李四"], ["王五"]]))
    assert table.n_rows == 3
    assert table.conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone() == (3,)


def test_溯源列记着这行是哪来的():
    """出了问题要能追回去："这行的数字是哪张表第几行来的"。"""
    table = build_query_table(_result(["姓名"], [["张三"]]))
    assert table.conn.execute(
        f"SELECT 来源文件, 来源行 FROM {TABLE_NAME}"
    ).fetchone() == ("甲学院.xlsx", 2)


def test_来源行的行号是数字_不是字符串():
    """**这里踩过一次坑，钉死它。**

    一开始两个溯源列都按 TEXT 建。行号是整数，插进 TEXT 列会被 SQLite
    悄悄转成字符串 ``'2'``——没报错，但 ``来源行 > 100`` 就变成
    按文字比大小，而文字比较里 ``"9" > "100"``。

    这列还是**每一行都有值**的，比别的列更容易被查到，
    所以这个坑的命中率其实很高。
    """
    table = build_query_table(_result(["姓名"], [["张三"], ["李四"], ["王五"]]))
    assert table.conn.execute(
        f"SELECT typeof(来源行) FROM {TABLE_NAME} LIMIT 1"
    ).fetchone() == ("integer",)


def test_空结果也能建表_查询返回0行():
    """没有数据时不该炸，而是"查得到，但是 0 行"。

    这样上层就不用到处写"表存不存在"的判断。
    """
    table = build_query_table(_result(["姓名"], []))
    assert table.n_rows == 0
    assert table.conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone() == (0,)


def test_完全没有字段时不炸():
    table = build_query_table(_result([], []))
    assert table.n_rows == 0
    assert table.fields == []


def test_建表前是写操作_建完才装闸():
    """顺序不能反：装闸之后连 CREATE TABLE 都会失败。

    这条是**防止有人"顺手"把装闸挪到建表前面**——那样表根本建不出来。
    建表的 SQL 是程序自己写的、不是模型写的，不需要那道闸；
    闸是给"模型写的查询"装的。
    """
    table = build_query_table(_result(["姓名"], [["张三"]]))
    # 表建好了、数据也在
    assert table.n_rows == 1


# --------------------------------------------------------------------------
# 类型推断（治"数字按文字比大小"）
# --------------------------------------------------------------------------

def test_整列都是整数就按数字存():
    table = build_query_table(_result(["课时"], [[128], [64], [256]]))
    assert table.types["课时"] == "INTEGER"


def test_有小数就按实数存():
    table = build_query_table(_result(["课时"], [[128.5], [64]]))
    assert table.types["课时"] == "REAL"


def test_数字列能正确比大小():
    """**这条是整个类型推断的意义所在。**

    如果按文字存，``"9" > "100"`` 是 True —— 9 个学生的排在 100 个前面。
    不报错、看着像正常结果，但你信了它就会算错事。
    """
    table = build_query_table(_result(["学生数"], [[9], [100], [25]]))
    got = table.conn.execute(
        f"SELECT 学生数 FROM {TABLE_NAME} WHERE 学生数 > 20 ORDER BY 学生数"
    ).fetchall()
    assert got == [(25,), (100,)]


def test_文字列不会被当成数字():
    """学号、工号这类必须留在文字里。

    存成数字会丢掉开头的 0（``007`` → ``7``），那是**把数据改坏了**，
    比排序不灵严重得多。
    """
    table = build_query_table(_result(["学号"], [["007"], ["010"]]))
    assert table.types["学号"] == "TEXT"
    assert table.conn.execute(f"SELECT 学号 FROM {TABLE_NAME} ORDER BY 学号").fetchall() == [
        ("007",),
        ("010",),
    ]


def test_一列里混着数字和文字就整列按文字存():
    """规则从紧：拿不准的宁可原样保留。

    跟 pipeline 里判断列性质是同一个道理——单个格子说明不了整列是什么。
    """
    table = build_query_table(_result(["备注"], [[120], ["暂无"]]))
    assert table.types["备注"] == "TEXT"


def test_空值不影响类型判断():
    table = build_query_table(_result(["课时"], [[128], [None], [64]]))
    assert table.types["课时"] == "INTEGER"


def test_全是空值的列按文字存():
    table = build_query_table(_result(["备注"], [[None], [None]]))
    assert table.types["备注"] == "TEXT"


def test_真假值不会被当成1和0():
    """``True`` 在 Python 里是 ``int`` 的子类，不拦的话会存成 1，
    查询结果里就冒出个莫名其妙的数字。"""
    table = build_query_table(_result(["在职"], [[True], [False]]))
    assert table.types["在职"] == "TEXT"
    assert table.conn.execute(f"SELECT 在职 FROM {TABLE_NAME}").fetchall() == [("True",), ("False",)]


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------

def test_日期存成ISO文字_能正确比大小():
    """ISO 格式（2026-09-01）的好处：按文字比就是按时间比。"""
    table = build_query_table(_result(["入职日期"], [[date(2026, 9, 1)], [date(2020, 1, 15)]]))
    assert table.types["入职日期"] == "TEXT"
    got = table.conn.execute(
        f"SELECT 入职日期 FROM {TABLE_NAME} WHERE 入职日期 > '2025-01-01'"
    ).fetchall()
    assert got == [("2026-09-01",)]


def test_日期不触发Python的弃用警告():
    """Python 3.12 起 sqlite3 自带的日期适配器已废弃。

    显式转成字符串就不会碰到它。这条测试就是盯着别哪天又依赖回去。
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        build_query_table(_result(["日期"], [[date(2026, 9, 1)]]))


def test_带时分秒的日期也存得下():
    table = build_query_table(_result(["时间"], [[datetime(2026, 9, 1, 10, 30)]]))
    value = table.conn.execute(f"SELECT 时间 FROM {TABLE_NAME}").fetchone()[0]
    assert value.startswith("2026-09-01") and "10:30" in value


# --------------------------------------------------------------------------
# 列名里的怪字符
# --------------------------------------------------------------------------

def test_列名里有双引号也能建表():
    """列名叫 ``我的"备注"`` 这种。双引号在 SQL 里要写两遍转义，
    不处理的话建表直接语法错误——而这是**建表失败**，
    整个提问功能都用不了。"""
    table = build_query_table(_result(['我的"备注"'], [["嗨"]]))
    assert table.conn.execute(f'SELECT "我的""备注""" FROM {TABLE_NAME}').fetchone() == ("嗨",)


def test_列名里有空格也能查():
    table = build_query_table(_result(["指导教师 姓名"], [["张三"]]))
    assert table.conn.execute(
        f'SELECT "指导教师 姓名" FROM {TABLE_NAME}'
    ).fetchone() == ("张三",)


# --------------------------------------------------------------------------
# 脱敏：唯一的出口
# --------------------------------------------------------------------------

def test_样本里的身份证和手机号一定打码():
    """样本是**每次提问都会发出去**的东西，比查询结果发得还频繁。"""
    table = build_query_table(
        _result(
            ["证件号", "电话"],
            [["330102199001011234", "13812345678"]],
        )
    )
    text = table.schema_text(samples_per_column=3)
    assert "330102199001011234" not in text
    assert "13812345678" not in text
    assert "3301**********1234" in text


def test_表里的值保持完整_只有发给模型的那份打码():
    """你自己在界面上看到的必须是完整原文。

    打码只作用于"发给模型的那份"——这条边界搞反了，
    用户就再也看不到自己的真实数据了。
    """
    table = build_query_table(
        _result(["证件号"], [["330102199001011234"]]),
        privacy=PrivacySettings(),
    )
    stored = table.conn.execute(f"SELECT 证件号 FROM {TABLE_NAME}").fetchone()[0]
    assert stored == "330102199001011234"


def test_关掉的脱敏开关真的生效():
    """以前 pipeline 和 mapper 各手抄一份开关，抄漏了导致开关形同虚设。

    这里钉死：关掉手机号打码，样本里就该看到完整手机号。
    """
    values = ["13812345678"]
    opened = mask_samples(values, 5, PrivacySettings(mask_phone=False))
    closed = mask_samples(values, 5, PrivacySettings(mask_phone=True))
    assert opened == ["13812345678"]
    assert closed == ["138****5678"]


def test_样本会跳过空值并去重():
    got = mask_samples(["张三", None, "", "  ", "张三", "李四"], 5, PrivacySettings())
    assert got == ["张三", "李四"]


def test_样本数量按要多少给多少():
    assert mask_samples(["a", "b", "c", "d"], 2, PrivacySettings()) == ["a", "b"]
    assert mask_samples(["a", "b"], 0, PrivacySettings()) == []


def test_姓名列的打码是留姓():
    assert mask_name("张三") == "张*"
    assert mask_name("欧阳修") == "欧**"
    assert mask_name("李") == "李"


def test_默认不打码姓名():
    """用户选的是"不打码"——不打码"张三有多少学生"才答得出来。"""
    table = build_query_table(_result(["姓名"], [["张三"]]))
    assert "张三" in table.schema_text()


def test_开了姓名打码之后样本里就看不到全名了():
    table = build_query_table(_result(["姓名"], [["张三"]]), mask_name=True)
    text = table.schema_text()
    assert "张三" not in text
    assert "张*" in text


def test_开姓名打码时身份证照样打码():
    """两个开关是叠加的，不是一个顶掉另一个。"""
    table = build_query_table(
        _result(["姓名", "证件号"], [["张三", "330102199001011234"]]),
        mask_name=True,
    )
    text = table.schema_text()
    assert "张三" not in text
    assert "330102199001011234" not in text


# --------------------------------------------------------------------------
# 表结构说明
# --------------------------------------------------------------------------

def test_表结构说明里有表名和总行数():
    table = build_query_table(_result(["姓名"], [["张三"], ["李四"]]))
    text = table.schema_text()
    assert TABLE_NAME in text
    assert "2" in text


def test_表结构说明里每列都提到():
    table = build_query_table(_result(["姓名", "课时"], [["张三", 128]]))
    text = table.schema_text()
    for name in table.columns:
        assert name in text


def test_表结构说明提醒了列名要用双引号():
    """模型爱用单引号包列名，那会静默返回假数据。提示词里必须叮嘱一句。"""
    table = build_query_table(_result(["姓名"], [["张三"]]))
    assert "双引号" in table.schema_text()


def test_样本个数给0就不给样本():
    table = build_query_table(_result(["姓名"], [["张三"]]))
    text = table.schema_text(samples_per_column=0)
    assert "张三" not in text
    assert "姓名" in text


# --------------------------------------------------------------------------
# 只读闸确实装上了
# --------------------------------------------------------------------------

def test_建好的表自动就是只读的():
    """**不用调用方记得装闸**——表一建好就已经上了锁。

    这条是防止有人把 ``install_readonly_guard`` 从 ``build_query_table``
    里挪走、改成"让调用方自己装"。忘了装的表现是：一切正常，
    只是模型哪天写出一条 UPDATE 就**真把数据改了**，
    而且不会有人发现。
    """
    table = build_query_table(_result(["姓名"], [["张三"]]))
    for sql in (
        f"UPDATE {TABLE_NAME} SET 姓名 = '李四'",
        f"DELETE FROM {TABLE_NAME}",
        f"DROP TABLE {TABLE_NAME}",
        f"INSERT INTO {TABLE_NAME} VALUES ('李四', 'x', 1)",
    ):
        with pytest.raises(sqlite3.Error):
            table.conn.execute(sql)
    # 挡完之后数据还在
    assert table.conn.execute(f"SELECT 姓名 FROM {TABLE_NAME}").fetchall() == [("张三",)]


def test_空表的连接也是只读的():
    """一条数据都没有时走的是另一条分支，闸不能漏装。"""
    table = build_query_table(_result(["姓名"], []))
    with pytest.raises(sqlite3.Error):
        table.conn.execute("CREATE TABLE 偷偷建的 (a TEXT)")


def test_关掉连接之后不报错():
    """关闭失败不该吵用户——数据是内存里的，关了就是没了，没有残留。"""
    table = build_query_table(_result(["姓名"], [["张三"]]))
    table.close()
    table.close()  # 关两次也不该炸
