"""内存消息缓存（查询）+ SQLite 落盘（持久化）+ WebSocket 事件总线。"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Optional

from .models import ChatMessage, Platform, DIRECTION_OUT
from .persistence import SqliteStore

logger = logging.getLogger(__name__)


class MessageStore:
    """内存里保存最近窗口（查询索引），写入同步落盘；启动时从库恢复。"""

    def __init__(self, max_size: int = 2000, db: Optional[SqliteStore] = None) -> None:
        self._messages: deque[ChatMessage] = deque(maxlen=max_size)
        self._index: dict[str, ChatMessage] = {}
        self._db = db

    def restore(self) -> int:
        """启动时从 SQLite 恢复最近窗口（含未确认草稿）。返回恢复条数。"""
        if self._db is None:
            return 0
        msgs = self._db.load_recent(self._messages.maxlen or 2000)
        n = 0
        for m in msgs:
            if m.id in self._index:
                continue
            self._messages.append(m)
            self._index[m.id] = m
            n += 1
        if n:
            logger.info("[store] 已从数据库恢复 %d 条消息（含待确认草稿）", n)
        return n

    def add(self, m: ChatMessage) -> ChatMessage:
        if m.id in self._index:  # 幂等，防止重复处理
            return self._index[m.id]
        if len(self._messages) == self._messages.maxlen:
            old = self._messages[0]
            self._index.pop(old.id, None)
        self._messages.append(m)
        self._index[m.id] = m
        if self._db is not None:
            self._db.upsert_message(m)
        return m

    def update(self, m: ChatMessage) -> None:
        """对象字段（status/content/meta）变更后同步落盘；内存里是同一对象引用。"""
        if self._db is not None:
            self._db.upsert_message(m)

    def audit(self, actor: str, action: str, message_id: str, detail: str = "") -> None:
        if self._db is not None:
            self._db.insert_audit(actor, action, message_id, detail)

    def get(self, msg_id: str) -> Optional[ChatMessage]:
        return self._index.get(msg_id)

    def set_status(self, msg_id: str, status: str) -> Optional[ChatMessage]:
        m = self._index.get(msg_id)
        if m is not None:
            m.status = status
        return m

    def history(self, platform: Platform | str, chat_id: str, limit: int = 12) -> list[ChatMessage]:
        """某会话最近 limit 条消息（两个方向都有，时间升序）。"""
        p = platform.value if isinstance(platform, Platform) else str(platform)
        items = [m for m in self._messages if m.platform.value == p and m.chat_id == chat_id]
        return items[-limit:]

    def list_messages(
        self,
        platform: str | None = None,
        chat_id: str | None = None,
        direction: str | None = None,
        limit: int = 200,
    ) -> list[ChatMessage]:
        items = list(self._messages)
        if platform:
            items = [m for m in items if m.platform.value == platform]
        if chat_id:
            items = [m for m in items if m.chat_id == chat_id]
        if direction:
            items = [m for m in items if m.direction == direction]
        return items[-limit:]

    def replies(self, status: str | None = None, limit: int = 100) -> list[ChatMessage]:
        items = [m for m in self._messages if m.direction == DIRECTION_OUT]
        if status:
            items = [m for m in items if m.status == status]
        return items[-limit:]

    def count(self) -> int:
        return len(self._messages)


class EventBus:
    """向所有 WebSocket 订阅者广播事件。"""

    def __init__(self, queue_size: int = 256) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # 慢消费者直接丢弃，不阻塞主流程
                logger.warning("事件队列已满，丢弃事件: %s", event.get("type"))
