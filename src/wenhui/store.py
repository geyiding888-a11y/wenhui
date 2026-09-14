"""SQLite 存储：记住"这个表头 AI 判断过什么"。

## 为什么要缓存

两个理由，都实在：

1. **省钱**。同一个学校的表每学期都长一个样。第一次问过 AI，
   第二次直接查表，一分钱不花。
2. **越用越准**。用户在界面上把 AI 的判断改正过一次之后，
   这个改正会被写进缓存——下次同样的表头，**直接按你教的对齐**。

## 一个文件搞定

用 SQLite 而不是 MySQL：这是单机工具，装数据库服务是纯粹的负担。
整个库就是 `data/wenhui.db` 一个文件，删了也只是"重新学一遍"。

> ⚠️ 这个库里**只存列名和映射关系，不存任何表格数据**。
> 所以它不含个人信息，删掉也不影响你手上的结果文件。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import CACHE_DB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mapping_cache (
    signature   TEXT PRIMARY KEY,   -- 表头指纹
    mapping     TEXT NOT NULL,      -- JSON：列名 -> 标准字段
    source      TEXT NOT NULL,      -- 'llm' | 'rule' | 'user'（用户手工改的）
    confidence  TEXT,               -- JSON：列名 -> 把握程度 0~1
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    n_files     INTEGER NOT NULL DEFAULT 0,
    n_records   INTEGER NOT NULL DEFAULT 0,
    n_issues    INTEGER NOT NULL DEFAULT 0,
    cost_cny    REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS query_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asked_at    TEXT NOT NULL,
    question    TEXT NOT NULL DEFAULT '',
    cost_cny    REAL NOT NULL DEFAULT 0,
    n_turns     INTEGER NOT NULL DEFAULT 0
);
"""


def signature_of(columns: list[str]) -> str:
    """给一组表头算指纹。

    列名的**顺序和大小写不敏感、空格不敏感**——因为"单位 / 姓名"和
    "姓名 / 单位"其实是同一张表，应该共用缓存。
    """
    normalized = sorted(
        "".join(str(c).split()).casefold() for c in columns if str(c).strip()
    )
    joined = "\x1f".join(normalized)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


@dataclass
class CacheEntry:
    mapping: dict[str, str | None]
    source: str
    #: 每一列当初的把握程度。**必须一起存下来**——不然"上次没把握、
    #: 需要你确认"的列，这次会被当成"确定无疑"，再也不会提醒你了。
    confidence: dict[str, float] = field(default_factory=dict)


class Store:
    """缓存与运行记录的读写。可以在多处复用同一个实例。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or CACHE_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            # 老版本建的库没有 confidence 列。就地补上，别让用户去删文件。
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(mapping_cache)")
            }
            if "confidence" not in columns:
                conn.execute("ALTER TABLE mapping_cache ADD COLUMN confidence TEXT")

            # 老版本把"规则兜底"的结果也存了进来，而且没记把握程度——
            # 那些条目下次会被当成"确定无疑"，把本该问 AI、本该提醒用户确认的
            # 列全部静音。现在规则结果一律不存，所以这些残留条目直接清掉：
            # 留着只有坏处（省不了钱，因为规则本来就免费）。
            conn.execute("DELETE FROM mapping_cache WHERE source = 'rule'")
            conn.commit()

    # ---------------------------------------------------------------- 映射缓存

    def get_mapping(self, signature: str) -> CacheEntry | None:
        """取缓存的映射；取到就把命中次数 +1。"""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT mapping, source, confidence FROM mapping_cache WHERE signature = ?",
                (signature,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE mapping_cache SET hit_count = hit_count + 1 WHERE signature = ?",
                (signature,),
            )
            conn.commit()
        try:
            mapping = json.loads(row["mapping"])
            confidence = json.loads(row["confidence"]) if row["confidence"] else {}
        except json.JSONDecodeError:
            return None
        return CacheEntry(mapping=mapping, source=row["source"], confidence=confidence)

    def put_mapping(
        self,
        signature: str,
        mapping: dict[str, str | None],
        source: str = "llm",
        confidence: dict[str, float] | None = None,
    ) -> None:
        """写入/更新映射。

        ``source`` 用 ``"user"`` 表示这是用户手工确认过的——
        界面上要优先展示"这是你自己定的"，而且**不许被后来的 AI 结果覆盖**。

        ``confidence`` 要如实一起存：它是"下次还要不要提醒用户确认"的依据。
        存成 1.0 就等于谎称"我很有把握"，那些没把握的列会被永久静音。
        """
        now = datetime.now().isoformat(timespec="seconds")
        payload = json.dumps(mapping, ensure_ascii=False)
        conf_payload = json.dumps(confidence or {}, ensure_ascii=False)
        with closing(self._connect()) as conn:
            existing = conn.execute(
                "SELECT source FROM mapping_cache WHERE signature = ?", (signature,)
            ).fetchone()

            # 用户手工定的映射优先级最高，AI 结果不许覆盖
            if existing is not None and existing["source"] == "user" and source != "user":
                return

            conn.execute(
                """
                INSERT INTO mapping_cache
                    (signature, mapping, source, confidence, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(signature) DO UPDATE SET
                    mapping = excluded.mapping,
                    source = excluded.source,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
                """,
                (signature, payload, source, conf_payload, now, now),
            )
            conn.commit()

    def forget(self, signature: str) -> bool:
        """让某条缓存失效（用户点"重新问一次 AI"时用）。"""
        with closing(self._connect()) as conn:
            cursor = conn.execute("DELETE FROM mapping_cache WHERE signature = ?", (signature,))
            conn.commit()
            return cursor.rowcount > 0

    def forget_all(self) -> int:
        """忘掉所有记住的对应关系，回到"从零开始学"。

        给用户留的一条后路：万一哪次确认点错了、把错误的对应关系记牢了，
        得有个地方能一笔勾销，而不是让他去删数据库文件。
        """
        with closing(self._connect()) as conn:
            cursor = conn.execute("DELETE FROM mapping_cache")
            conn.commit()
            return cursor.rowcount

    def cache_size(self) -> int:
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM mapping_cache").fetchone()[0])

    # ---------------------------------------------------------------- 运行记录

    def start_run(self, n_files: int) -> int:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "INSERT INTO runs (started_at, n_files) VALUES (?, ?)",
                (datetime.now().isoformat(timespec="seconds"), n_files),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, n_records: int, n_issues: int, cost_cny: float) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE runs
                   SET finished_at = ?, n_records = ?, n_issues = ?, cost_cny = ?
                 WHERE id = ?
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    n_records,
                    n_issues,
                    cost_cny,
                    run_id,
                ),
            )
            conn.commit()

    def total_cost(self) -> float:
        """历史累计花费，给用户一个"我一共花过多少钱"的交代。"""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COALESCE(SUM(cost_cny), 0) AS total FROM runs").fetchone()
            return float(row["total"])

    # ---------------------------------------------------------------- 问答花费

    def record_query(self, question: str, cost_cny: float, n_turns: int = 0) -> int:
        """记一次提问的花费。

        **为什么不塞进 ``runs`` 表**：``runs`` 是"一次汇总"的语义
        （有 ``n_files`` / ``n_records`` 这些字段）。一次提问既没有文件数
        也没有记录条数，硬塞进去会让"累计花费"这个数字说不清是哪来的——
        用户在侧边栏看到"累计花费 ¥3.2"，没法判断是汇总花的还是聊天花的。

        单独一张表，就能分开显示"汇总累计 ¥x / 问答累计 ¥y"。
        顺带也解释了那个 bug：以前 agent 的花销**根本没进过账**，
        问了 20 个问题，这里也不会有任何记录。

        :param question: 用户问的那句话。**存之前要截断**——用户可能
            粘贴一大段文字进来，没必要整个存下来。
        :returns: 新记录的行号。
        """
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "INSERT INTO query_usage (asked_at, question, cost_cny, n_turns) "
                "VALUES (?, ?, ?, ?)",
                (
                    datetime.now().isoformat(timespec="seconds"),
                    question.strip()[:200],
                    float(cost_cny),
                    int(n_turns),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def query_cost(self) -> float:
        """问答功能的累计花费。和 :meth:`total_cost`（汇总累计）分开算。"""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_cny), 0) AS total FROM query_usage"
            ).fetchone()
            return float(row["total"])

    def n_queries(self) -> int:
        """一共问过几次。"""
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM query_usage").fetchone()[0])
