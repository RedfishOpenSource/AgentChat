"""微信连接器。

提供三种实现：
- WeChatReplicaConnector (mode=replica，推荐)：基于 wechatauto-replica 库，
  支持微信 4.x（4.1.12+，实测 4.1.15.13）。
    * 监听：本地数据库增量轮询（Listener，水位落盘，重启不重复推送），不受窗口影响
    * 发送：UIA+OCR 混合驱动（仅在人工确认草稿后被调用）
- WeChatConnector (mode=wxauto)：经典 wxauto UI 自动化，仅支持微信 3.9.x（保留兼容）
- MockConnector (mode=mock)：模拟，用于开发与测试
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Sequence

from ..config import ConnectorSettings
from ..models import ChatMessage, Platform, now_ms
from .base import BaseConnector, ConnectorStartError, EmitFn

logger = logging.getLogger(__name__)

# wechatauto 消息类型名 → 标准类型；未知类型按 text 处理（尽力传递，不静默丢弃）
_TYPE_MAP = {
    "文本": "text",
    "图片": "image",
    "语音": "voice",
    "视频": "video",
    "动画表情": "emoji",
    "位置": "location",
    "文件/链接/卡片": "file",
    "音视频通话": "call",
    "系统消息": "system",
    "红包": "redpacket",
}


def _parse_replica_msg(
    msg: dict,
    chat_name: str,
    username: str,
    nick_resolver: Callable[[Any], str],
    self_nick: str = "",
) -> ChatMessage | None:
    """把 wechatauto Listener 的 msg 字典转换为 ChatMessage。

    msg 字段（实测）：sender_id / sender_username / content / type(友好名) / create_time
    self_nick：当前登录账号昵称，用于群聊 @我 检测。回声过滤由 _is_echo 在回调层完成。
    """
    content = str(msg.get("content") or "").strip()
    if not content:
        return None  # 系统行/空行跳过
    sender_id = msg.get("sender_id")
    sender_username = str(msg.get("sender_username") or "")
    msg_type = _TYPE_MAP.get(str(msg.get("type") or ""), "text")
    is_group = username.endswith("@chatroom")
    at_me = bool(is_group and self_nick and f"@{self_nick}" in content)
    try:
        sender = (nick_resolver(sender_id) or "").strip() or f"用户{sender_id}"
    except Exception:  # noqa: BLE001
        sender = f"用户{sender_id}"
    ts = msg.get("create_time") or 0
    try:
        ts = int(float(ts) * 1000)
    except (TypeError, ValueError):
        ts = now_ms()
    return ChatMessage(
        platform=Platform.WECHAT,
        chat_id=chat_name,
        sender=sender,
        content=content,
        ts=ts,
        msg_type=msg_type,
        at_me=at_me if is_group else None,
        is_group=is_group,
        meta={"replica": {"username": username, "sender_id": str(sender_id),
                          "sender_username": sender_username,
                          "raw_type": str(msg.get("type") or "")}},
    )


def _is_echo(
    msg: dict,
    username: str,
    self_wxid: str,
    recent_sent: Sequence[tuple[str, float]],
) -> bool:
    """判断是否为自己发出的回声消息（需丢弃）。

    - 普通会话：自己发的消息一律视为回声（机器人回复后会写进本地库）
    - 文件传输助手（username=filehelper）：那是用户自己的备忘录，用户手打的消息必须放行；
      仅当内容与最近 120 秒内机器人已发送的回复完全一致时才视为回声。
    """
    sender_username = str(msg.get("sender_username") or "")
    if not self_wxid or sender_username != self_wxid:
        return False  # 他人消息
    if username != "filehelper":
        return True
    content = str(msg.get("content") or "").strip()
    now = time.monotonic()
    return any(c == content and now - t <= 120 for c, t in recent_sent)


class WeChatReplicaConnector(BaseConnector):
    """微信 4.x 连接器（wechatauto-replica：本地库增量轮询 + UIA/OCR 发送）。"""

    platform = Platform.WECHAT
    DEFAULT_CHAT = "文件传输助手"

    def __init__(self, settings: ConnectorSettings, on_messages: EmitFn) -> None:
        super().__init__(settings, on_messages)
        self._db: Any = None
        self._listener: Any = None
        self._gui: Any = None  # 懒初始化：仅人工确认草稿后才需要 GUI 发送
        self._buffer: list[ChatMessage] = []
        self._lock = threading.Lock()
        self._self_wxid = ""
        self._self_nick = ""
        self._recent_sent: deque[tuple[str, float]] = deque(maxlen=50)  # (内容, 时间)，回声比对用

    # ---------- 监听 ----------
    async def on_start(self) -> None:
        try:
            from wechatauto.db import Listener, WeChatDB  # noqa: PLC0415 —— 延迟导入
        except ImportError as exc:
            raise ConnectorStartError(
                "未安装 wechatauto-replica（uv pip install wechatauto-replica），或改用 mode=mock"
            ) from exc

        self._db = WeChatDB()
        try:
            info = self._db.get_self_info() or {}
            self._self_wxid = str(info.get("username") or "")
            self._self_nick = str(info.get("nick_name") or "")
            logger.info("[wechat] 当前登录账号: %s (%s)", self._self_nick, self._self_wxid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[wechat] 获取自身身份失败，无法过滤回声/检测群聊@： %s", exc)
        interval = max(1.0, float(self.settings.poll_interval or 1.0))
        self._listener = Listener(self._db, interval=interval)

        chats = list(self.settings.listen_chats or [])
        if not chats:
            chats = [self.DEFAULT_CHAT]
            logger.warning("[wechat] 未配置 listen_chats，默认监听「%s」（建议在配置中显式指定）", self.DEFAULT_CHAT)

        resolved = 0
        for raw in chats:
            username = self._resolve_chat(raw)
            if not username:
                logger.warning("[wechat] 会话解析失败，跳过: %s", raw)
                continue
            self._listener.add_listener(username, self._make_callback(raw, username))
            logger.info("[wechat] 监听会话: %s (username=%s)", raw, username)
            resolved += 1
        if resolved == 0:
            raise ConnectorStartError("没有可监听的微信会话，请检查 listen_chats 配置")

        self._listener.start()
        logger.info("[wechat] 数据库监听已启动 (interval=%ss)", interval)

    def _resolve_chat(self, raw: str) -> str | None:
        """把配置里的会话名解析为 username（昵称/备注/username 均可）。"""
        try:
            for s in self._db.get_sessions(limit=500):
                if s.get("username") == raw:
                    return raw
        except Exception:  # noqa: BLE001
            pass
        try:
            hits = self._db.search_contact(raw)
            if hits:
                return hits[0]["username"]
        except Exception:  # noqa: BLE001
            pass
        return raw or None  # 兜底：原样返回（demo 同款策略）

    def _nick(self, sender_id: Any) -> str:
        try:
            return self._db.get_nickname(sender_id) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _make_callback(self, chat_name: str, username: str) -> Callable:
        def on_msg(msg: dict, _lst: Any) -> None:
            if _is_echo(msg, username, self._self_wxid, self._recent_sent):
                logger.debug("[wechat] 已过滤回声消息: %s", str(msg.get("content"))[:30])
                return
            cm = _parse_replica_msg(msg, chat_name, username, self._nick, self._self_nick)
            if cm is None:
                return
            if cm.is_group:
                logger.info(
                    "[wechat] 群消息: %s@%s type=%s @我=%s: %s",
                    cm.sender, cm.chat_id, cm.msg_type, cm.at_me, cm.content[:40],
                )
            else:
                logger.info("[wechat] 收到新消息: %s@%s: %s", cm.sender, cm.chat_id, cm.content[:40])
            with self._lock:
                self._buffer.append(cm)

        return on_msg

    async def poll(self) -> list[ChatMessage]:
        with self._lock:
            if not self._buffer:
                return []
            msgs, self._buffer = self._buffer, []
        return msgs

    # ---------- 发送（仅人工确认草稿后到达此处） ----------
    async def send_reply(self, message: ChatMessage) -> None:
        if self._gui is None:
            from wechatauto import WeChat  # noqa: PLC0415 —— GUI 发送路径懒初始化
            self._gui = WeChat()
        try:
            resp = self._gui.SendMsg(message.content, who=message.chat_id)
            ok = getattr(resp, "ok", None)
            if ok is False:
                logger.warning("[wechat] 发送返回失败: %s", getattr(resp, "message", resp))
                return
            self._recent_sent.append((message.content.strip(), time.monotonic()))  # 回声比对记录
            logger.info("[wechat] 已发送回复 -> chat=%s: %s", message.chat_id, message.content[:50])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[wechat] 发送失败: %s", exc)

    async def on_stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # noqa: BLE001
                pass
            self._listener = None


class WeChatConnector(BaseConnector):
    """经典 wxauto 连接器（mode=wxauto，仅支持微信 3.9.x，保留兼容）。"""

    platform = Platform.WECHAT

    def __init__(self, settings: ConnectorSettings, on_messages: EmitFn) -> None:
        super().__init__(settings, on_messages)
        self._wx: Any = None
        self._listened: dict[str, Any] = {}

    async def on_start(self) -> None:
        try:
            from wxauto import WeChat  # noqa: PLC0415 —— 延迟导入
        except ImportError as exc:
            raise ConnectorStartError(
                "未安装 wxauto（注意：开源版仅支持微信 3.9.x；微信 4.x 请改用 mode=replica）"
            ) from exc
        self._wx = WeChat()
        self._listened = {}
        for chat in self.settings.listen_chats:
            try:
                self._listened[chat] = self._wx.AddListenChat(who=chat)
                logger.info("[wechat] 开始监听会话: %s", chat)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[wechat] 添加监听会话失败 %s: %s", chat, exc)
        if not self._listened:
            logger.warning("[wechat] 未配置 listen_chats，将收不到新消息")

    async def poll(self) -> list[ChatMessage]:
        out: list[ChatMessage] = []
        if self._wx is None:
            return out
        try:
            packages = self._wx.GetListenMessage() or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[wechat] GetListenMessage 失败: %s", exc)
            return out
        for chat_obj, items in packages.items():
            chat_name = (
                getattr(chat_obj, "chat_name", None)
                or getattr(chat_obj, "name", None)
                or str(chat_obj)
            )
            for item in items:
                if getattr(item, "attr", "") == "self":
                    continue  # 跳过自己发的消息
                content = getattr(item, "content", None) or str(item)
                sender = getattr(item, "sender", None) or chat_name
                out.append(
                    ChatMessage(
                        platform=Platform.WECHAT,
                        chat_id=str(chat_name),
                        sender=str(sender),
                        content=str(content),
                    )
                )
        return out

    async def send_reply(self, message: ChatMessage) -> None:
        if self._wx is None:
            logger.warning("[wechat] 连接器未启动，回复仅记录不发送")
            return
        ctrl = self._listened.get(message.chat_id)
        if ctrl is not None:
            ctrl.SendMsg(message.content)
        else:
            self._wx.SendMsg(msg=message.content, who=message.chat_id)
        logger.info("[wechat] 已发送回复 -> chat=%s", message.chat_id)
