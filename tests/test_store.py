"""缓存的读写：把握程度必须**原样存、原样取**。

为什么专门测这个：缓存最怕的不是"没记住"，而是"记岔了"。
把"我拿不准"记成"我确定"，用户就再也不会被提醒——
而且他没有任何办法察觉，因为界面上写的是"全部对上了"。
"""

from __future__ import annotations

import sqlite3

from wenhui.store import Store, signature_of


def test_存进去的把握程度取得回来(tmp_path):
    store = Store(tmp_path / "t.db")
    signature = signature_of(["单位", "姓名"])

    store.put_mapping(
        signature,
        {"单位": "单位", "序号": None},
        source="llm",
        confidence={"单位": 0.93, "序号": 0.4},
    )

    entry = store.get_mapping(signature)
    assert entry is not None
    assert entry.mapping == {"单位": "单位", "序号": None}
    assert entry.confidence == {"单位": 0.93, "序号": 0.4}


def test_没记把握程度的老缓存取得回来且不报错(tmp_path):
    """用户手工确认的映射不记把握程度；读的时候要给个空字典，不能崩。"""
    store = Store(tmp_path / "t.db")
    signature = signature_of(["单位"])
    store.put_mapping(signature, {"单位": "单位"}, source="user")

    entry = store.get_mapping(signature)
    assert entry is not None
    assert entry.source == "user"
    assert entry.confidence == {}


def test_规则兜底的结果不再被缓存(tmp_path):
    """老版本存过一批"规则结果"，它们没记把握程度，会把该确认的列静音掉。

    新版本一律不存规则结果，所以残留的老条目要在打开库时清掉——
    而用户自己确认过的、AI 判断过的，一条都不能动。
    """
    path = tmp_path / "old.db"
    # 造一个"老版本"的库：没有 confidence 列，且混着三种来源
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE mapping_cache (
                signature TEXT PRIMARY KEY, mapping TEXT NOT NULL, source TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        for i, source in enumerate(("rule", "llm", "user")):
            conn.execute(
                "INSERT INTO mapping_cache VALUES (?, ?, ?, '', '', 0)",
                (f"sig{i}", '{"单位": "单位"}', source),
            )
        conn.commit()

    store = Store(path)  # 打开时自动迁移

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(mapping_cache)")}
        left = {row[0] for row in conn.execute("SELECT source FROM mapping_cache")}
    assert "confidence" in columns
    assert left == {"llm", "user"}


def test_忘掉全部之后回到一张白纸(tmp_path):
    """用户点错了确认、把错误关系记牢了，得有一条后路。"""
    store = Store(tmp_path / "t.db")
    store.put_mapping(signature_of(["单位"]), {"单位": "单位"}, source="user")
    store.put_mapping(signature_of(["姓名"]), {"姓名": "姓名"}, source="llm")
    assert store.cache_size() == 2

    assert store.forget_all() == 2
    assert store.cache_size() == 0


def test_用户确认过的映射不会被后来的AI结果覆盖(tmp_path):
    store = Store(tmp_path / "t.db")
    signature = signature_of(["单位"])
    store.put_mapping(signature, {"单位": "单位"}, source="user")

    store.put_mapping(signature, {"单位": "部门"}, source="llm", confidence={"单位": 0.9})

    entry = store.get_mapping(signature)
    assert entry is not None
    assert entry.source == "user"
    assert entry.mapping == {"单位": "单位"}


def test_表头指纹对顺序和大小写不敏感():
    """同一张表，列的顺序换了、空格多了，还是同一张表，该共用缓存。"""
    assert signature_of(["单位", " 姓名 "]) == signature_of(["姓名", "单位"])


# --------------------------------------------------------------------------
# 估价的依据
# --------------------------------------------------------------------------

def test_只报真花过钱的那一次(tmp_path):
    """助手重跑前要报个价，价是照"上次花了多少"估的。

    **"不花钱试跑"也会在库里留一行**（它照样读了文件、写了总表），
    但花费是 0。不过滤的话，用户试跑完再让助手重跑，
    卡片上会写"上次花了 ¥0.0000"——报了个假数，比不报还糟。
    """
    store = Store(tmp_path / "t.db")

    dry = store.start_run(3)
    store.finish_run(dry, 900, 0, 0.0)          # 试跑，不花钱

    assert store.last_paid_run() is None, "把试跑那次的 0 元当成花了钱"

    paid = store.start_run(3)
    store.finish_run(paid, 900, 2, 0.0050)

    assert store.last_paid_run() == (3, 0.0050)


def test_一次都没跑过时不瞎猜(tmp_path):
    """没跑过就返回 None，让界面直说"说不准"——**不能编一个数出来**。"""
    assert Store(tmp_path / "t.db").last_paid_run() is None


def test_跑到一半没收尾的那次不算数(tmp_path):
    """``finished_at`` 还是空的那一行，说明这次跑崩了。

    拿它估价的话，会按一次**没跑完**的运行报价——那个数字没有意义。
    """
    store = Store(tmp_path / "t.db")
    store.start_run(3)                          # 只开了头，没收尾
    assert store.last_paid_run() is None


def test_报价取最近一次而不是最贵的一次(tmp_path):
    """报"上次"才符合用户的心理预期——他心里比的是"我刚花的那笔"。"""
    store = Store(tmp_path / "t.db")
    first = store.start_run(3)
    store.finish_run(first, 900, 0, 0.0900)
    second = store.start_run(5)
    store.finish_run(second, 1500, 1, 0.0120)

    assert store.last_paid_run() == (5, 0.0120)
