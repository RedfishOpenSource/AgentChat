"""SQLite 持久层：消息落盘 + 审计日志。

设计：
- 单连接 + 线程锁（当前管线全部在事件循环线程内写，锁仅作保险）
- WAL 模式（文件库）：读写不互斥
- messages 表全量历史；内存 MessageStore 只是最近窗口的查询缓存
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any, Optional

from .models import ChatMessage, now_ms

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    platform   TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    sender     TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    direction  TEXT NOT NULL,
    status     TEXT NOT NULL,
    reply_to   TEXT,
    meta       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_messages_chat  ON messages(platform, chat_id, ts);
CREATE INDEX IF NOT EXISTS idx_messages_dir   ON messages(direction, status);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);
"""


class SqliteStore:
    def __init__(self, db_path: str = "chatagent.db") -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if db_path != ":memory:":
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.DatabaseError:  # pragma: no cover
                    pass
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        logger.info("[db] SQLite 已就绪: %s", db_path)

    # ---------- messages ----------
    def upsert_message(self, m: ChatMessage) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO messages (id, platform, chat_id, sender, content, ts, direction, status, reply_to, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    content = excluded.content,
                    meta = excluded.meta
                """,
                (
                    m.id, m.platform.value, m.chat_id, m.sender, m.content, m.ts,
                    m.direction, m.status, m.reply_to,
                    json.dumps(m.meta, ensure_ascii=False),
                ),
            )
            self._conn.commit()

    def load_recent(self, limit: int = 2000) -> list[ChatMessage]:
        """按时间升序返回最近 limit 条（供启动时恢复内存缓存）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        out: list[ChatMessage] = []
        for r in reversed(rows):
            try:
                meta = json.loads(r["meta"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            out.append(
                ChatMessage(
                    id=r["id"], platform=r["platform"], chat_id=r["chat_id"],
                    sender=r["sender"], content=r["content"], ts=r["ts"],
                    direction=r["direction"], status=r["status"],
                    reply_to=r["reply_to"], meta=meta,
                )
            )
        return out

    def count_messages(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()
        return int(row["c"]) if row else 0

    # ---------- audit ----------
    def insert_audit(self, actor: str, action: str, message_id: str, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log (ts, actor, action, message_id, detail) VALUES (?, ?, ?, ?, ?)",
                (now_ms(), actor, action, message_id, detail),
            )
            self._conn.commit()

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass
