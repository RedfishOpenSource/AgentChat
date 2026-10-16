"""告警中心：异常去重、恢复通知、可插拔通知渠道。

原则：
- 同一 key 的告警只在状态变化时通知一次（激活 / 恢复）
- 通知渠道默认关闭（off）——告警消息是发给你自己的（微信文件传输助手 / 钉钉自有会话），
  不涉及他人；需要时在配置中显式开启
- 通知有最小间隔节流，防止风暴
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    key: str
    message: str
    level: str = "error"
    ts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "message": self.message, "level": self.level, "ts": self.ts}


class AlertManager:
    def __init__(
        self,
        notifier: Any = None,
        min_interval: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._notifier = notifier
        self._min_interval = min_interval
        self._clock = clock
        self._active: dict[str, Alert] = {}
        self._last_notify = -1e18
        self._lock = asyncio.Lock()

    # ---------- 状态查询 ----------
    def set_notifier(self, notifier: Any) -> None:
        """热更新通知渠道（配置热更新用）。"""
        self._notifier = notifier

    def active_list(self) -> list[dict[str, Any]]:
        return [a.to_dict() for a in self._active.values()]

    def is_active(self, key: str) -> bool:
        return key in self._active

    # ---------- 触发 / 恢复 ----------
    def raise_alert(self, key: str, message: str, level: str = "error") -> bool:
        """激活告警；同一 key 已激活时只刷新信息，不重复通知。返回是否为新告警。"""
        now = int(time.time())
        if key in self._active:
            self._active[key].message = message
            self._active[key].ts = now
            return False
        alert = Alert(key=key, message=message, level=level, ts=now)
        self._active[key] = alert
        logger.warning("[alert] 告警激活 [%s] %s", key, message)
        self._fire(f"⚠️ ChatAgent 告警 [{key}] {message}")
        return True

    def resolve_alert(self, key: str) -> bool:
        """解除告警；若确实处于激活态，发送恢复通知。返回是否发生了解除。"""
        alert = self._active.pop(key, None)
        if alert is None:
            return False
        logger.info("[alert] 告警恢复 [%s] %s", key, alert.message)
        self._fire(f"✅ ChatAgent 已恢复 [{key}]")
        return True

    # ---------- 通知 ----------
    def _fire(self, text: str) -> None:
        """节流后异步发送通知（无渠道或节流期内只记日志）。"""
        now = self._clock()
        if self._notifier is None:
            return
        if now - self._last_notify < self._min_interval:
            logger.warning("[alert] 通知节流中，跳过: %s", text)
            return
        self._last_notify = now
        try:
            asyncio.get_running_loop().create_task(self._send(text))
        except RuntimeError:  # 无事件循环（如测试/启动期），同步跳过
            logger.info("[alert] (无事件循环，仅记录) %s", text)

    async def _send(self, text: str) -> None:
        try:
            await self._notifier.send(text)
            logger.info("[alert] 告警通知已发送: %s", text[:60])
        except Exception as exc:  # noqa: BLE001 —— 通知失败不影响主流程
            logger.warning("[alert] 告警通知发送失败: %s", exc)


# ---------------- 通知渠道 ----------------
class WeChatFilehelperNotifier:
    """微信文件传输助手（自己的备忘录会话，零外发）。"""

    name = "wechat-filehelper"

    def __init__(self) -> None:
        self._wx: Any = None

    async def send(self, text: str) -> None:
        if self._wx is None:
            from wechatauto import WeChat  # noqa: PLC0415

            self._wx = WeChat()
        await asyncio.to_thread(self._wx.SendMsg, text, "文件传输助手")


class DingTalkSelfNotifier:
    """钉钉自有会话（chat_id 由配置指定，应为发给自己的会话或自建群）。"""

    name = "dingtalk-self"

    def __init__(self, dws_bin: str = "dws", chat_id: str = "") -> None:
        self.dws_bin = dws_bin or "dws"
        self.chat_id = chat_id

    async def send(self, text: str) -> None:
        if not self.chat_id:
            raise RuntimeError("未配置 alert_dingtalk_chat_id")
        cmd = [
            self.dws_bin, "chat", "+messages-send",
            "--as", "user", "--chat-id", self.chat_id,
            "--text", text,
            "--idempotency-key", uuid.uuid4().hex,
            "--yes",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"dws 发送失败(退出码 {proc.returncode})")


def build_notifier(channel: str, dingtalk_chat_id: str = "", dws_bin: str = "dws") -> Optional[Any]:
    """按配置构建通知渠道；off/未知返回 None。"""
    if channel == "wechat-filehelper":
        return WeChatFilehelperNotifier()
    if channel == "dingtalk-self":
        return DingTalkSelfNotifier(dws_bin=dws_bin, chat_id=dingtalk_chat_id)
    if channel in ("", "off"):
        return None
    logger.warning("[alert] 未知告警渠道 %s，已忽略", channel)
    return None
