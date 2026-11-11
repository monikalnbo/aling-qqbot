"""SQLite 存储层：中期摘要 + 长期事实条目。

表结构刻意做得简单，方便人工核对与备份：
- session_summaries : 每个会话(umo)一行，存滚动摘要与其覆盖到的轮次序号
- user_facts        : 按 (session_umo, qq_id) 归档的长期事实条目
- turn_counters     : 每个会话的轮次计数（用于「每 N 轮抽取一次」）
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_summaries (
    umo            TEXT PRIMARY KEY,
    summary        TEXT NOT NULL DEFAULT '',
    covered_turns  INTEGER NOT NULL DEFAULT 0,
    updated_at     REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    umo         TEXT NOT NULL,
    qq_id       TEXT NOT NULL,
    display     TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_user_facts_owner ON user_facts (umo, qq_id);
CREATE TABLE IF NOT EXISTS turn_counters (
    umo     TEXT PRIMARY KEY,
    turns   INTEGER NOT NULL DEFAULT 0
);
"""


class MemoryStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 中期摘要 ----------
    def get_summary(self, umo: str) -> tuple[str, int]:
        row = self._conn.execute(
            "SELECT summary, covered_turns FROM session_summaries WHERE umo=?", (umo,)
        ).fetchone()
        if not row:
            return "", 0
        return row["summary"], row["covered_turns"]

    def set_summary(self, umo: str, summary: str, covered_turns: int) -> None:
        self._conn.execute(
            """INSERT INTO session_summaries (umo, summary, covered_turns, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(umo) DO UPDATE SET
                 summary=excluded.summary,
                 covered_turns=excluded.covered_turns,
                 updated_at=excluded.updated_at""",
            (umo, summary, covered_turns, time.time()),
        )
        self._conn.commit()

    # ---------- 长期事实 ----------
    @staticmethod
    def _similar(a: str, b: str) -> float:
        """字符二元组 Jaccard，够用来判「同一件事换个说法」。"""
        def grams(x: str) -> set[str]:
            x = "".join(ch for ch in x if ch.isalnum())
            return {x[i : i + 2] for i in range(len(x) - 1)} if len(x) > 1 else {x}

        ga, gb = grams(a), grams(b)
        if not ga or not gb:
            return 0.0
        return len(ga & gb) / len(ga | gb)

    @staticmethod
    def _cover(a: str, b: str) -> float:
        """较短一条的二元组有多大比例出现在较长一条里（处理「同一件事多说一句」）。"""
        def grams(x: str) -> set[str]:
            x = "".join(ch for ch in x if ch.isalnum())
            return {x[i : i + 2] for i in range(len(x) - 1)}

        ga, gb = grams(a), grams(b)
        if not ga or not gb:
            return 0.0
        short, long_ = (ga, gb) if len(ga) <= len(gb) else (gb, ga)
        return len(short & long_) / len(short)

    def add_fact(
        self, umo: str, qq_id: str, display: str, content: str
    ) -> int | None:
        """写入一条事实；同一 QQ 下内容完全相同或高度相似的只保留一条。"""
        content = content.strip()
        if not content:
            return None
        row = self._conn.execute(
            "SELECT id, content FROM user_facts WHERE qq_id=?", (qq_id,)
        ).fetchall()
        for r in row:
            sim = self._similar(r["content"], content)
            cover = sim and self._cover(r["content"], content)
            if r["content"] == content or sim >= 0.8 or cover >= 0.72:
                self._conn.execute(
                    "UPDATE user_facts SET updated_at=?, display=?, content=? WHERE id=?",
                    (time.time(), display, content, r["id"]),
                )
                self._conn.commit()
                return int(r["id"])
        now = time.time()
        cur = self._conn.execute(
            """INSERT INTO user_facts (umo, qq_id, display, content, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (umo, qq_id, display, content, now, now),
        )
        self._conn.commit()
        return int(cur.lastrowid) if cur.lastrowid else None

    def list_facts(self, qq_id: str) -> list[sqlite3.Row]:
        """按 QQ 号取该用户的全部长期条目（跨会话、跨群、跨私聊）。"""
        return list(
            self._conn.execute(
                "SELECT * FROM user_facts WHERE qq_id=? ORDER BY updated_at DESC",
                (qq_id,),
            ).fetchall()
        )

    def count_facts(self, qq_id: str | None = None) -> int:
        if qq_id is None:
            return int(
                self._conn.execute("SELECT COUNT(*) AS c FROM user_facts").fetchone()["c"]
            )
        return int(
            self._conn.execute(
                "SELECT COUNT(*) AS c FROM user_facts WHERE qq_id=?", (qq_id,)
            ).fetchone()["c"]
        )

    def delete_fact(self, fact_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM user_facts WHERE id=?", (fact_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def clear_user(self, qq_id: str) -> int:
        cur = self._conn.execute("DELETE FROM user_facts WHERE qq_id=?", (qq_id,))
        self._conn.commit()
        return cur.rowcount

    # ---------- 轮次计数 ----------
    def bump_turn(self, umo: str) -> int:
        self._conn.execute(
            """INSERT INTO turn_counters (umo, turns) VALUES (?, 1)
               ON CONFLICT(umo) DO UPDATE SET turns = turns + 1""",
            (umo,),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT turns FROM turn_counters WHERE umo=?", (umo,)
        ).fetchone()
        return int(row["turns"]) if row else 0
