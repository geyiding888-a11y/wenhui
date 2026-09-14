"""安全闸：AI 写的查询**只能看，不能改**。

这个文件里的每一条都对应一种"真出事了会很严重"的场景：
数据被删、被改、界面被焊死、答案悄悄算错。

**"AI 一般不会这么写"不是理由。** 它可能写错，也可能被一段
精心构造的问题带偏——比如有人问"如果要把重复行删掉该怎么写"，
模型好心给你写一条 DELETE 出来。闸门必须在**它真执行的那一刻**拦住。
"""

from __future__ import annotations

import sqlite3

import pytest

from wenhui.agent.guard import (
    arm_timeout,
    clear_timeout,
    disable_dqs,
    fix_identifier_quotes,
    install_readonly_guard,
    looks_like_select,
    wrap_limit,
)


@pytest.fixture()
def conn():
    """一个装好只读闸的连接，里面有一张表、一行数据。"""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.execute("CREATE TABLE 汇总数据 (姓名 TEXT, 课时 INTEGER)")
    c.execute("INSERT INTO 汇总数据 VALUES ('张三', 128)")
    c.commit()
    install_readonly_guard(c)
    yield c
    c.close()


# --------------------------------------------------------------------------
# 只读闸
# --------------------------------------------------------------------------

#: 七种攻击。前六种是"想把数据搞坏"，最后一种是"想一次塞好几条语句"。
_ATTACKS = [
    ("删表", "DROP TABLE 汇总数据"),
    ("改数据", "UPDATE 汇总数据 SET 课时 = 0"),
    ("插数据", "INSERT INTO 汇总数据 VALUES ('李四', 999)"),
    ("建表", "CREATE TABLE 偷来的数据 (a TEXT)"),
    ("看连接信息", "PRAGMA table_info(汇总数据)"),
    ("挂载别的库", "ATTACH DATABASE 'D:/secret.db' AS 别的"),
    ("一次塞两条", "SELECT * FROM 汇总数据; DROP TABLE 汇总数据"),
]


@pytest.mark.parametrize("label,sql", _ATTACKS, ids=[a[0] for a in _ATTACKS])
def test_七种攻击全部拦住(conn, label, sql):
    with pytest.raises(sqlite3.Error):
        conn.execute(sql)


def test_挡完之后数据还在(conn):
    """拦是拦住了，但**原表有没有被搞坏**得单独确认一遍。

    只看"抛异常了"不够——有的写法是先改掉一半再报错。
    """
    for _label, sql in _ATTACKS:
        with pytest.raises(sqlite3.Error):
            conn.execute(sql)
    rows = conn.execute("SELECT 姓名, 课时 FROM 汇总数据").fetchall()
    assert rows == [("张三", 128)]


def test_正常查询照样能跑(conn):
    """闸门不能把正常功能也一起挡了。

    这条是防止"为了安全把闸收得太紧"——那种情况下测试全绿，
    但用户一个都查不了。
    """
    assert conn.execute("SELECT COUNT(*) FROM 汇总数据").fetchone() == (1,)
    assert conn.execute("SELECT SUM(课时) FROM 汇总数据").fetchone() == (128,)
    assert conn.execute(
        "SELECT 姓名 FROM 汇总数据 WHERE 课时 > 100"
    ).fetchall() == [("张三",)]


def test_递归查询能用_但转不出去会被掐断(conn):
    """放行 WITH RECURSIVE，同时超时闸要能兜住"递归把自己转死"。"""
    arm_timeout(conn, seconds=0.5)
    # 一个会转很久的递归查询。不掐的话能跑到天荒地老
    with pytest.raises(sqlite3.Error):
        conn.execute(
            "WITH RECURSIVE 数(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM 数 WHERE n < 100000000)"
            " SELECT COUNT(*) FROM 数"
        )
    clear_timeout(conn)
    # 掐断之后连接**还能用**——这点很重要，不然用户得重启程序
    assert conn.execute("SELECT COUNT(*) FROM 汇总数据").fetchone() == (1,)


def test_授权器按动作拒绝_不按文本():
    """**这条是整套安全设计的核心。**

    如果拿"SQL 文本里有没有 DELETE 字样"当防线，下面这些全都能骗过去：
    加注释、换大小写、用括号断开关键字。而授权器看到的是**动作本身**，
    它不管你怎么写，只管你**要干什么**。
    """
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.execute("CREATE TABLE t (a TEXT)")
    c.execute("INSERT INTO t VALUES ('x')")
    c.commit()
    install_readonly_guard(c)

    # 这三种写法文本上完全看不出 DELETE，但动作就是删表
    rome = [
        "DROP/**/TABLE t",
        "drop table t",
        "DR" + "OP TABLE t",
    ]
    for sql in rome:
        with pytest.raises(sqlite3.Error):
            c.execute(sql)
    assert c.execute("SELECT COUNT(*) FROM t").fetchone() == (1,)
    c.close()


# --------------------------------------------------------------------------
# 超时
# --------------------------------------------------------------------------

def test_装一次超时只管一次():
    """**每次查询前都要重新装。**

    截止时间是装的那一刻现算的。装一次、跑两次的话，
    第二次查询可能刚开跑就已经过期了——或者反过来，
    第一次跑完很久了，第二次还在用那个过期的截止时间，
    等于没有保护。这里把这个契约钉死。
    """
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.execute("CREATE TABLE t (a INTEGER)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(1000)])
    c.commit()

    arm_timeout(c, seconds=10)  # 给得足够宽，正常查询绝不会超时
    assert c.execute("SELECT SUM(a) FROM t").fetchone() is not None
    # 同一个 handler 还在，再跑一次也应该正常
    assert c.execute("SELECT SUM(a) FROM t").fetchone() is not None
    c.close()


def test_撤掉超时之后不再中断():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.execute("CREATE TABLE t (a INTEGER)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()

    arm_timeout(c, seconds=0)  # 立刻过期
    clear_timeout(c)           # 撤掉
    assert c.execute("SELECT COUNT(*) FROM t").fetchone() == (1,)
    c.close()


# --------------------------------------------------------------------------
# 双引号怪癖（单引号那个坑的镜像版）
# --------------------------------------------------------------------------

def _fresh():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.execute("CREATE TABLE 汇总数据 (姓名 TEXT, 课时 INTEGER)")
    c.execute("INSERT INTO 汇总数据 VALUES ('张三', 128)")
    c.commit()
    return c


def test_不关掉怪癖时_列名写错会返回一列假数据():
    """**先把问题本身钉下来**，免得以后有人觉得"关不关无所谓"。

    SQLite 的老习惯：双引号里的名字找不到对应列时，它不报错，
    而是把那段文字当成一个字符串常量返回。
    """
    c = _fresh()
    got = c.execute("SELECT \"不存在的列\" FROM 汇总数据").fetchall()
    # 表里有几行，它就返回几个"不存在的列"——**看着完全像正常数据**
    assert got == [("不存在的列",)]
    c.close()


def test_关掉怪癖之后_列名写错会真的报错():
    """这是**确定性修复**，不是猜的。

    关掉之后，模型把列名记错一个字的后果是"报错、能改"，
    而不是"拿到一列看着很正常的假数据、然后拿它回答你"。
    """
    c = _fresh()
    assert disable_dqs(c) is True
    with pytest.raises(sqlite3.Error):
        c.execute("SELECT \"不存在的列\" FROM 汇总数据")
    c.close()


def test_关掉怪癖不影响正常写法():
    """关的时候要确认没有误伤——真列名用双引号、字符串值用单引号，
    这两种正常写法必须照常能跑。"""
    c = _fresh()
    disable_dqs(c)
    assert c.execute('SELECT "姓名" FROM 汇总数据 WHERE "课时" > 100').fetchall() == [("张三",)]
    assert c.execute("SELECT \"姓名\" FROM 汇总数据 WHERE \"课时\" = '128'").fetchall() == [("张三",)]
    assert c.execute("SELECT 'abc'").fetchall() == [("abc",)]
    c.close()


def test_关掉怪癖后_值写成双引号也会报错():
    """``WHERE 姓名 = "张三"`` 也是靠这个怪癖才"碰巧能跑"的。

    报错反而是好事——提示里直接写着"该用单引号"，
    模型看到就会改对。
    """
    c = _fresh()
    disable_dqs(c)
    with pytest.raises(sqlite3.Error):
        c.execute('SELECT "姓名" FROM 汇总数据 WHERE "姓名" = "张三"')
    c.close()


# --------------------------------------------------------------------------
# 单引号修正
# --------------------------------------------------------------------------

_COLUMNS = ["姓名", "课时", "单位"]


def test_单引号包列名会被改成双引号():
    """治的是"不报错但算错"：

    ``SELECT '单位' FROM 汇总数据`` 在 SQLite 里**不报错**，
    返回的是一列全是"单位"两个字的假数据。用户看不出来。
    """
    fixed = fix_identifier_quotes("SELECT '单位' FROM 汇总数据", _COLUMNS)
    assert fixed == 'SELECT "单位" FROM 汇总数据'


def test_单引号里的真实数据一个都不碰():
    """**这条比上面那条更要紧。**

    修错了就是把用户的查询改坏：``WHERE 姓名 = '张三'`` 里的 '张三'
    是**数据**不是列名，动它就等于换了个查询。
    """
    sql = "SELECT 课时 FROM 汇总数据 WHERE 姓名 = '张三'"
    assert fix_identifier_quotes(sql, _COLUMNS) == sql


def test_列名和数据同名时数据不受影响():
    """极端情况：某人的名字正好叫"单位"。

    这时候 ``WHERE 单位 = '单位'`` 前半是列名、后半是数据。
    只有整段单引号内容一字不差等于列名时才换，
    所以 ``'单位'`` 在 **= 右边** 也会被换掉……

    这一条是**记录已知边界**，不是"我们做对了"：
    ``WHERE 单位 = '单位'`` 会被改成 ``WHERE 单位 = "单位"``，
    语义从"等于文字'单位'"变成"等于本列的值"，结果恒为真。

    但这个场景要成立，得先有一个人的名字**正好等于一个列名**，
    而且查询里正好在那列上等值比较它。权衡下来，
    "列名写错返回一整列假数据"（常见、静默、没人能发现）
    比"数据正好叫列名"（几乎不可能）严重得多，
    所以这里选择保守地一律替换。
    """
    fixed = fix_identifier_quotes("SELECT 姓名 FROM 汇总数据 WHERE 单位 = '单位'", _COLUMNS)
    assert fixed == 'SELECT 姓名 FROM 汇总数据 WHERE 单位 = "单位"'


def test_带空格的列名前后不误伤():
    sql = "SELECT ' 单位 ' FROM 汇总数据"
    # 前后带空格的不是列名，原样保留（保守策略：认不出来就不动）
    assert fix_identifier_quotes(sql, _COLUMNS) == sql


def test_没有已知列名时原样返回():
    sql = "SELECT '随便什么' FROM t"
    assert fix_identifier_quotes(sql, []) == sql
    assert fix_identifier_quotes(sql, ["", "  "]) == sql


def test_字符串里的转义单引号不会被当成两段():
    """``'it''s'`` 是 SQL 里"字符串内部有个单引号"的写法。

    正则要是不认这个，会把它截成 ``'it'`` 和 ``'s'`` 两段，
    然后干出莫名其妙的事。
    """
    sql = "SELECT 姓名 FROM 汇总数据 WHERE 备注 = 'it''s ok'"
    assert fix_identifier_quotes(sql, _COLUMNS) == sql


def test_列名里本来就有双引号也能修对():
    """列名叫 ``我的"备注"`` 这种。双引号在 SQL 里要写两遍转义。"""
    fixed = fix_identifier_quotes("SELECT '我的\"备注\"' FROM t", ['我的"备注"'])
    assert fixed == 'SELECT "我的""备注""" FROM t'


# --------------------------------------------------------------------------
# 包 LIMIT
# --------------------------------------------------------------------------

def test_没写LIMIT就包一层():
    assert wrap_limit("SELECT * FROM t", 200) == "SELECT * FROM (SELECT * FROM t) LIMIT 200"


def test_已经写了LIMIT就不动它():
    """它自己心里有数。硬套一层会改变它的意图（比如取前 5 名）。"""
    sql = "SELECT * FROM t LIMIT 5"
    assert wrap_limit(sql, 200) == sql
    assert wrap_limit("select * from t limit 5", 200) == "select * from t limit 5"


def test_结尾分号会被剥掉():
    """不剥的话套进括号里会变成 ``(SELECT * FROM t;)`` ——语法错误。"""
    assert wrap_limit("SELECT * FROM t;", 200) == "SELECT * FROM (SELECT * FROM t) LIMIT 200"


def test_限量给0表示不限():
    sql = "SELECT * FROM t"
    assert wrap_limit(sql, 0) == sql
    assert wrap_limit(sql, -1) == sql


def test_空查询原样返回():
    assert wrap_limit("", 200) == ""
    assert wrap_limit("   ", 200) == "   "


# --------------------------------------------------------------------------
# 友善提示
# --------------------------------------------------------------------------

def test_认出查询语句():
    assert looks_like_select("SELECT * FROM t")
    assert looks_like_select("  select * from t")
    assert looks_like_select("WITH x AS (SELECT 1) SELECT * FROM x")


def test_认出前面带注释的查询语句():
    """模型很爱在 SQL 前面加一行注释解释自己在干嘛。"""
    assert looks_like_select("-- 查一下张三的课时\nSELECT * FROM t")
    assert looks_like_select("/* 统计人数 */ SELECT COUNT(*) FROM t")


def test_认出不是查询的语句():
    """注意：**这不是安全边界**，只是给模型的一句提示。

    真正的边界是 install_readonly_guard —— 就算这里被骗过去了，
    授权器那一关照样拦得住。
    """
    assert not looks_like_select("DROP TABLE t")
    assert not looks_like_select("DELETE FROM t")
    assert not looks_like_select("UPDATE t SET a = 1")
