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
