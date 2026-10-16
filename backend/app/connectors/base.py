"""聊天平台连接器基类：监听新消息 + 发送回复。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, ClassVar, Sequence

from ..config import ConnectorSettings
from ..models import ChatMessage, Platform

logger = logging.getLogger(__name__)

EmitFn = Callable[[Sequence[ChatMessage]], Awaitable[None]]


class ConnectorStartError(RuntimeError):
    """连接器启动失败（如缺少依赖、无法连接外部工具）。"""


class BaseConnector(ABC):
    platform: ClassVar[Platform]

    def __init__(self, settings: ConnectorSettings, on_messages: EmitFn) -> None:
        self.settings = settings
        self._on_messages = on_messages
        self.status = "stopped"  # stopped | running | error
        self.last_error: str = ""
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def mode(self) -> str:
        return self.settings.mode

    def info(self) -> dict:
        return {
            "platform": self.platform.value,
            "mode": self.mode,
            "status": self.status,
            "last_error": self.last_error,
            "poll_interval": self.settings.poll_interval,
        }

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        if self.status == "running":
            return
        self.last_error = ""
        try:
            await self.on_start()
        except Exception as exc:
            self.status = "error"
            self.last_error = str(exc)
            logger.error("[%s] 连接器启动失败: %s", self.platform.value, exc)
            raise ConnectorStartError(str(exc)) from exc
        self._stop_event.clear()
        self.status = "running"
        self._task = asyncio.create_task(self._poll_loop(), name=f"connector-{self.platform.value}")
        logger.info("[%s] 连接器已启动 (mode=%s)", self.platform.value, self.mode)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.on_stop()
        self.status = "stopped"
        logger.info("[%s] 连接器已停止", self.platform.value)

    async def _poll_loop(self) -> None:
        """监听循环：一旦 poll 到新消息就回调 on_messages（触发生成回复）。"""
        try:
            while not self._stop_event.is_set():
                try:
                    msgs = await self.poll()
                    if msgs:
                        await self._on_messages(msgs)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.exception("[%s] poll 异常: %s", self.platform.value, exc)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=max(0.01, self.settings.poll_interval)
                    )
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("[%s] 监听循环退出", self.platform.value)

    # ---------- 子类钩子 ----------
    async def on_start(self) -> None:
        """真实连接器在此初始化外部资源；失败直接抛异常。"""

    async def on_stop(self) -> None:
        """释放资源。"""

    @abstractmethod
    async def poll(self) -> list[ChatMessage]:
        """拉取一批新消息；无新消息返回空列表。"""

    @abstractmethod
    async def send_reply(self, message: ChatMessage) -> None:
        """把回复发送到对应平台。"""
