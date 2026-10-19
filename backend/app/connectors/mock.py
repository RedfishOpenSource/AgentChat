"""Mock 连接器：用于开发与端到端模拟测试。

- inject() 注入“收到的新消息”（供前端模拟面板 / REST API / 测试调用）
- send_reply() 只记录到 sent 列表，便于断言与展示
"""
from __future__ import annotations

import logging
from typing import Sequence

from ..config import ConnectorSettings
from ..models import ChatMessage, Platform
from .base import BaseConnector, EmitFn

logger = logging.getLogger(__name__)


class MockConnector(BaseConnector):
    def __init__(self, platform: Platform, settings: ConnectorSettings, on_messages: EmitFn) -> None:
        super().__init__(settings, on_messages)
        self.platform = platform  # type: ignore[misc]
        self._inbox: list[ChatMessage] = []
        self.sent: list[ChatMessage] = []

    async def inject(self, chat_id: str, sender: str, content: str, enqueue: bool = False,
                     msg_type: str = "text", is_group: bool | None = None,
                     at_me: bool | None = None) -> ChatMessage:
        """注入一条模拟的新消息。

        enqueue=False: 返回消息但不进入监听队列（调用方随后直接 ingest，时序确定）
        enqueue=True:  进入队列，由监听循环 poll 拾取（用于测试监听链路）
        """
        msg = ChatMessage(platform=self.platform, chat_id=chat_id, sender=sender, content=content,
                          msg_type=msg_type, is_group=is_group, at_me=at_me)
        if enqueue:
            self._inbox.append(msg)
        return msg

    async def poll(self) -> list[ChatMessage]:
        if not self._inbox:
            return []
        msgs, self._inbox = self._inbox, []
        return msgs

    async def send_reply(self, message: ChatMessage) -> bool:
        self.sent.append(message)
        logger.info(
            "[%s] (mock) 已发送回复 -> chat=%s: %s",
            self.platform.value, message.chat_id, message.content[:50],
        )
        return True


def is_mock_connector(conn: BaseConnector) -> bool:
    return isinstance(conn, MockConnector)


def _unused(*args: Sequence[ChatMessage]) -> None:  # pragma: no cover
    """占位，避免 lint 误报。"""
