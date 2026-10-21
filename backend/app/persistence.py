"""SQLite 持久层：消息落盘、审计日志和设置面板配置。

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
    meta       TEXT NOT NULL DEFAULT '{}',
    msg_type   TEXT NOT NULL DEFAULT 'text',
    at_me      INTEGER,
    is_group   INTEGER
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

CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_profiles (
    id          TEXT PRIMARY KEY,
    data        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_assignments (
    platform    TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    PRIMARY KEY (platform, chat_id)
);

CREATE TABLE IF NOT EXISTS session_resets (
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    token TEXT NOT NULL,
    reset_at INTEGER NOT NULL,
    PRIMARY KEY (platform, chat_id)
);

CREATE TABLE IF NOT EXISTS contact_configs (
    platform    TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (platform, chat_id)
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
            columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(messages)")}
            for name, declaration in (("msg_type", "TEXT NOT NULL DEFAULT 'text'"), ("at_me", "INTEGER"), ("is_group", "INTEGER")):
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")
            self._conn.commit()
        logger.info("[db] SQLite 已就绪: %s", db_path)

    def list_session_resets(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute('SELECT * FROM session_resets')]

    def save_session_reset(self, platform: str, chat_id: str, token: str, reset_at: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                'INSERT INTO session_resets(platform,chat_id,token,reset_at) VALUES(?,?,?,?) '
                'ON CONFLICT(platform,chat_id) DO UPDATE SET token=excluded.token, reset_at=excluded.reset_at',
                (platform, chat_id, token, reset_at),
            )

    # ---------- persistent settings ----------
    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM app_settings").fetchall()
        settings: dict[str, Any] = {}
        for row in rows:
            try:
                settings[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                logger.warning("[db] 忽略无效的持久化配置项: %s", row["key"])
        return settings

    def save_settings(self, settings: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(key, json.dumps(value, ensure_ascii=False)) for key, value in settings.items()],
            )

    # ---------- agent profiles and contact assignments ----------
    def list_agent_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM agent_profiles ORDER BY rowid").fetchall()
        profiles = []
        for row in rows:
            try:
                profiles.append(json.loads(row["data"]))
            except json.JSONDecodeError:
                logger.warning("[db] 忽略损坏的 Agent 配置")
        return profiles

    def save_agent_profile(self, profile: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO agent_profiles (id, data) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                (profile["id"], json.dumps(profile, ensure_ascii=False)),
            )

    def delete_agent_profile(self, agent_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM agent_profiles WHERE id = ?", (agent_id,))
            self._conn.execute("DELETE FROM agent_assignments WHERE agent_id = ?", (agent_id,))

    def list_agent_assignments(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT platform, chat_id, agent_id FROM agent_assignments ORDER BY platform, chat_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def assign_agent(self, platform: str, chat_id: str, agent_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO agent_assignments (platform, chat_id, agent_id) VALUES (?, ?, ?) "
                "ON CONFLICT(platform, chat_id) DO UPDATE SET agent_id = excluded.agent_id",
                (platform, chat_id, agent_id),
            )

    def remove_contact(self, platform: str, chat_id: str) -> None:
        """移除监听联系人时，在同一事务中清理绑定和联系人配置。"""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM agent_assignments WHERE platform = ? AND chat_id = ?", (platform, chat_id)
            )
            self._conn.execute(
                "DELETE FROM contact_configs WHERE platform = ? AND chat_id = ?", (platform, chat_id)
            )

    def unassign_agent(self, platform: str, chat_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM agent_assignments WHERE platform = ? AND chat_id = ?", (platform, chat_id)
            )

    # ---------- per-contact listen settings ----------
    def list_contact_configs(self) -> dict[str, dict[str, Any]]:
        """返回 {(platform, chat_id): config} 形式的联系人监听配置。"""
        with self._lock:
            rows = self._conn.execute("SELECT platform, chat_id, data FROM contact_configs").fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                out[(row["platform"], row["chat_id"])] = json.loads(row["data"])
            except json.JSONDecodeError:
                logger.warning("[db] 忽略损坏的联系人监听配置: %s/%s", row["platform"], row["chat_id"])
        return out

    def save_contact_config(self, platform: str, chat_id: str, config: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO contact_configs (platform, chat_id, data) VALUES (?, ?, ?) "
                "ON CONFLICT(platform, chat_id) DO UPDATE SET data = excluded.data",
                (platform, chat_id, json.dumps(config, ensure_ascii=False)),
            )

    def delete_contact_config(self, platform: str, chat_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM contact_configs WHERE platform = ? AND chat_id = ?", (platform, chat_id)
            )

    # ---------- messages ----------
    def upsert_message(self, m: ChatMessage) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO messages (id, platform, chat_id, sender, content, ts, direction, status, reply_to, meta, msg_type, at_me, is_group)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    content = excluded.content,
                    meta = excluded.meta,
                    msg_type = excluded.msg_type,
                    at_me = excluded.at_me,
                    is_group = excluded.is_group
                """,
                (
                    m.id, m.platform.value, m.chat_id, m.sender, m.content, m.ts,
                    m.direction, m.status, m.reply_to,
                    json.dumps(m.meta, ensure_ascii=False), m.msg_type,
                    None if m.at_me is None else int(m.at_me),
                    None if m.is_group is None else int(m.is_group),
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
                    msg_type=r["msg_type"],
                    at_me=None if r["at_me"] is None else bool(r["at_me"]),
                    is_group=None if r["is_group"] is None else bool(r["is_group"]),
                )
            )
        return out

    def get_message(self, message_id: str) -> ChatMessage | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return self._rows_to_messages([row])[0] if row else None

    def delete_chat_messages(self, platform: str, chat_id: str) -> int:
        """删除某会话的全部消息记录（含草稿与系统标记），返回删除条数。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM messages WHERE platform = ? AND chat_id = ?", (platform, chat_id)
            )
            return cur.rowcount

    def list_messages(self, platform: str | None = None, chat_id: str | None = None,
                      direction: str | None = None, status: str | None = None,
                      limit: int = 200, offset: int = 0) -> list[ChatMessage]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("platform", platform), ("chat_id", chat_id), ("direction", direction), ("status", status)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM messages{where} ORDER BY ts DESC, rowid DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(limit, 1000)), max(0, offset)),
            ).fetchall()
        return self._rows_to_messages(rows)

    def _rows_to_messages(self, rows) -> list[ChatMessage]:
        out: list[ChatMessage] = []
        for r in reversed(rows):
            try:
                meta = json.loads(r["meta"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            out.append(ChatMessage(
                id=r["id"], platform=r["platform"], chat_id=r["chat_id"], sender=r["sender"],
                content=r["content"], ts=r["ts"], direction=r["direction"], status=r["status"],
                reply_to=r["reply_to"], meta=meta, msg_type=r["msg_type"],
                at_me=None if r["at_me"] is None else bool(r["at_me"]),
                is_group=None if r["is_group"] is None else bool(r["is_group"]),
            ))
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
