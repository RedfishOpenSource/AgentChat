"""钉钉连接器。

提供两种实现：
- DingTalkCliConnector (mode=cli)：通用 CLI 约定（拉取命令 + 回复命令模板）
- DingTalkDwsConnector (mode=dws)：对接钉钉官方 DWS CLI（DingTalk Workspace CLI）

DWS 模式：
- 监听：`dws event +listen-im --kind <at-me|all-direct|all-group|group> --format ndjson`
  长连接子进程，stdout 输出 NDJSON 事件（混有 `[event]` 日志行，解析时过滤）
- 回复：优先引用回复 `dws chat +messages-reply --ref-msg-id <id> --group <cid> --content <text>`
        缺少原消息引用时回退直发 `dws chat +messages-send --as user --chat-id <cid> --text <text>`
  全部带 `--yes`（跳过交互确认）与 `--idempotency-key`（防重发）
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import re
import time
import uuid

from ..models import ChatMessage, Platform, gen_id, now_ms
from ..config import ConnectorSettings
from .base import BaseConnector, ConnectorStartError, EmitFn

logger = logging.getLogger(__name__)


def _parse_event_line(raw: str) -> ChatMessage | None:
    """解析 DWS 事件流的一行输出（NDJSON）。

    实测格式（flatten）：
      {"type":"user_im_message_receive_at", "message_id":"msgX", "conversation_id":"cidX",
       "sender":"赵丽阳", "sender_open_dingtalk_id":"...", "content":"@xx 内容",
       "event_time":1792121116051, ...}
    同时兼容信封格式（data 为业务 payload JSON 字符串/对象）与驼峰字段。
    `[event] ...` 等日志行、无法解析的行返回 None。
    """
    line = (raw or "").strip()
    if not line or not line.startswith("{"):
        return None  # [event] ready/bus/exited 等日志行
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    payload = obj
    data = obj.get("data")
    if isinstance(data, str):
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return None
    elif isinstance(data, dict):
        payload = data
    if not isinstance(payload, dict):
        return None

    # 正文：兼容 text 为对象（钉钉 Stream: {"content": "..."}）或字符串
    text = payload.get("text")
    if isinstance(text, dict):
        text = text.get("content")
    if text is None:
        text = payload.get("content") or ""
    raw_content = str(text).strip()
    if not raw_content:
        return None

    sender = (
        payload.get("senderNick")
        or payload.get("sender")
        or payload.get("senderNickName")
        or payload.get("senderStaffId")
        or payload.get("senderId")
        or ""
    )
    sender = str(sender)
    # 去除 @我 前缀（如 “@赵丽阳  内容”），但先保留原文判断触发场景。
    content = raw_content
    at_sender = bool(sender and re.search(rf"@{re.escape(sender)}(?:\s|$)", raw_content))
    if sender and content.startswith("@" + sender):
        content = content[len(sender) + 1:].strip()

    conversation_id = (
        payload.get("conversation_id")
        or payload.get("conversationId")
        or payload.get("openConversationId")
        or payload.get("chatId")
        or ""
    )
    message_id = (
        payload.get("message_id")
        or payload.get("msgId")
        or payload.get("messageId")
        or ""
    )
    title = payload.get("conversationTitle") or payload.get("conversationName") or ""
    event_type = str(obj.get("type") or payload.get("type") or "").lower()
    conversation_type = str(payload.get("conversationType") or payload.get("conversation_type") or "").lower()
    is_group = bool(
        "group" in event_type
        or "group" in conversation_type
        or event_type.endswith("_at")
    )
    explicit_at_me = payload.get("isAtMe", payload.get("at_me"))
    at_me = (bool(explicit_at_me) if explicit_at_me is not None else event_type.endswith("_at")) if is_group else None

    ts = (
        payload.get("event_time")
        or payload.get("timestamp")
        or payload.get("createAt")
        or payload.get("createTime")
        or obj.get("received_at_unix_ms")
        or 0
    )
    if isinstance(ts, str):
        try:
            ts = int(ts)
        except ValueError:
            ts = 0
    ts = int(ts) if ts else now_ms()
    if ts and ts < 10**12:  # 秒级时间戳转毫秒
        ts *= 1000

    # chat_id 优先用会话名（可读），稳定 ID 存入 meta 供回复使用
    chat_id = str(title) if title else (str(conversation_id) or "default")
    stable_id = "dws_" + hashlib.sha256(f"{conversation_id}:{message_id}".encode()).hexdigest()[:24] if message_id else None
    return ChatMessage(
        id=stable_id or gen_id("msg"),
        platform=Platform.DINGTALK,
        chat_id=chat_id,
        sender=str(sender),
        content=content,
        ts=ts,
        is_group=is_group,
        at_me=at_me,
        meta={
            "dws": {
                "message_id": str(message_id),
                "conversation_id": str(conversation_id),
                "title": str(title),
            }
        },
    )


def _build_reply_cmd(
    content: str,
    src: dict,
    dws_bin: str = "dws",
    reply_mode: str = "quote",
    idempotency_key: str | None = None,
) -> list[str]:
    """构造回复命令（create_subprocess_exec 参数列表，无 shell 转义问题）。

    quote 模式且具备原消息引用时用 +messages-reply；否则回退 +messages-send 直发。
    """
    key = idempotency_key or uuid.uuid4().hex
    message_id = str(src.get("message_id") or "")
    conversation_id = str(src.get("conversation_id") or "")

    if reply_mode == "quote" and message_id:
        cmd = [
            dws_bin, "chat", "+messages-reply",
            "--content", content,
            "--ref-msg-id", message_id,
            "--idempotency-key", key,
            "--yes",
        ]
        if conversation_id:
            cmd += ["--group", conversation_id]
        return cmd

    cmd = [
        dws_bin, "chat", "+messages-send",
        "--as", "user",
        "--text", content,
        "--idempotency-key", key,
        "--yes",
    ]
    if conversation_id:
        cmd += ["--chat-id", conversation_id]
    return cmd


class DingTalkDwsConnector(BaseConnector):
    """基于 DWS CLI 的钉钉连接器（事件流长连接 + 引用回复）。"""

    platform = Platform.DINGTALK
    RESTART_DELAY = 3.0
    CMD_TIMEOUT = 60.0
    CHAT_CACHE_TTL = 300.0  # 会话名 → openConversationId 缓存有效期（秒）

    def __init__(self, settings: ConnectorSettings, on_messages: EmitFn) -> None:
        super().__init__(settings, on_messages)
        self._procs: list[asyncio.subprocess.Process] = []
        self._tasks: list[asyncio.Task] = []
        self._buffer: list[ChatMessage] = []
        self._chat_cache: list[tuple[str, str]] = []
        self._chat_cache_at: float = 0.0
        self._chat_cache_task: asyncio.Task | None = None

    async def list_contacts(self) -> list[tuple[str, str]]:
        """当前用户加入的会话列表（会话名, openConversationId），供前端联系人弹窗选择。

        调用 `dws chat +chat-list`（群聊 + 单聊，单页 100 条，不自动翻页以免阻塞 UI）。
        仅 dws 模式可用；CLI 未安装/未登录/输出异常时返回空列表（上层回退消息记录建议）。
        """
        cmd = [
            self.settings.dws_bin or "dws", "chat", "+chat-list",
            "--types", "group,p2p", "--page-size", "100",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.CMD_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dingtalk] 拉取会话列表失败: %s", exc)
            return []
        if proc.returncode != 0:
            logger.warning("[dingtalk] 拉取会话列表失败（exit=%s）", proc.returncode)
            return []
        try:
            data = json.loads(stdout.decode("utf-8", "ignore"))
        except (ValueError, AttributeError):
            logger.warning("[dingtalk] 会话列表输出不是有效 JSON，忽略")
            return []
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for chat in (data or {}).get("chats") or []:
            if not isinstance(chat, dict):
                continue
            cid = str(chat.get("openConversationId") or chat.get("conversationId") or "").strip()
            name = str(chat.get("name") or chat.get("title") or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            out.append((name or cid, cid))
        return out

    # ---------- 生命周期 ----------
    async def on_start(self) -> None:
        targets: list[list[str]] = []
        kind = self.settings.listen_kind or "at-me"
        base_cmd = [self.settings.dws_bin or "dws", "event", "+listen-im", "--format", "ndjson", "--yes"]
        if kind == "group" and self.settings.listen_chats:
            for chat in self.settings.listen_chats:  # 每个群一个订阅进程
                targets.append(base_cmd + ["--kind", "group", "--chat-query", chat])
        else:
            targets.append(base_cmd + ["--kind", kind])

        for cmd in targets:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=1 << 20,
                )
            except FileNotFoundError as exc:
                raise ConnectorStartError(
                    f"找不到 DWS CLI（{self.settings.dws_bin or 'dws'}），请确认已安装并加入 PATH"
                ) from exc
            except Exception as exc:  # noqa: BLE001
                raise ConnectorStartError(f"启动 DWS 事件流失败: {exc}") from exc
            self._procs.append(proc)
            self._tasks.append(asyncio.create_task(self._read_loop(proc, cmd)))
        logger.info("[dingtalk] 已启动 %d 路 DWS 事件流订阅 (kind=%s)", len(self._procs), kind)

    async def on_stop(self) -> None:
        import contextlib

        for t in self._tasks:
            t.cancel()
        for proc in self._procs:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, asyncio.TimeoutError):
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=3)
        self._procs, self._tasks = [], []

    # ---------- 监听 ----------
    async def _read_loop(self, proc: asyncio.subprocess.Process, cmd: list[str]) -> None:
        """读取事件流子进程 stdout：NDJSON 事件 → 缓冲；进程意外退出自动重启。"""
        assert proc.stdout is not None
        try:
            while not self._stop_event.is_set():
                line = await proc.stdout.readline()
                if not line:  # EOF：子进程退出
                    if self._stop_event.is_set():
                        break
                    logger.warning(
                        "[dingtalk] 事件流进程退出(code=%s)，%ss 后重启: %s",
                        proc.returncode, self.RESTART_DELAY, " ".join(cmd[:4]),
                    )
                    await asyncio.sleep(self.RESTART_DELAY)
                    if self._stop_event.is_set():
                        break
                    new_proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        limit=1 << 20,
                    )
                    self._procs[self._procs.index(proc)] = new_proc
                    proc = new_proc
                    continue
                msg = _parse_event_line(line.decode("utf-8", "ignore"))
                if msg is not None:
                    self._buffer.append(msg)
                    logger.info("[dingtalk] 收到新消息: %s@%s: %s", msg.sender, msg.chat_id, msg.content[:40])
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.status = "error"
            self.last_error = f"事件流读取异常: {exc}"
            logger.exception("[dingtalk] 事件流读取异常: %s", exc)

    async def poll(self) -> list[ChatMessage]:
        self._maybe_refresh_chat_cache()
        if not self._buffer:
            return []
        msgs, self._buffer = self._buffer, []
        return msgs

    def _maybe_refresh_chat_cache(self) -> None:
        """缓存过期后异步刷新会话列表（供 username_by_name 反解显示名）。"""
        if time.monotonic() - self._chat_cache_at < self.CHAT_CACHE_TTL:
            return
        self._chat_cache_at = time.monotonic()
        if self._chat_cache_task is not None and not self._chat_cache_task.done():
            return
        self._chat_cache_task = asyncio.create_task(self._refresh_chat_cache())

    async def _refresh_chat_cache(self) -> None:
        try:
            self._chat_cache = await self.list_contacts()
        except Exception:  # noqa: BLE001
            logger.warning("[dingtalk] 刷新会话名缓存失败", exc_info=True)

    def username_by_name(self, name: str) -> str | None:
        """把会话名（群名/联系人名）反解为 openConversationId；解析不出返回 None。

        供白名单匹配使用：监听配置保存的是显示名，而 webhook 事件可能缺
        conversationTitle，此时消息 chat_id/meta 里只有 openConversationId。
        会话列表由 poll() 周期性后台刷新，此处同步读缓存，不阻塞消息处理。
        """
        key = (name or "").strip()
        if not key:
            return None
        for chat_name, cid in self._chat_cache:
            if chat_name.strip() == key and cid:
                return cid
        return None

    # ---------- 发送 ----------
    async def send_reply(self, message: ChatMessage) -> bool:
        src = (message.meta or {}).get("dws") or {}
        cmd = _build_reply_cmd(
            message.content, src,
            dws_bin=self.settings.dws_bin or "dws",
            reply_mode=self.settings.reply_mode or "quote",
            idempotency_key=message.id,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.CMD_TIMEOUT)
        except asyncio.TimeoutError as exc:
            if "proc" in locals() and proc.returncode is None:
                proc.kill()
                await proc.wait()
            logger.warning("[dingtalk] 回复命令结果不确定（超时）: %s", " ".join(cmd[:4]))
            raise RuntimeError("钉钉回复命令超时，发送结果不确定") from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dingtalk] 回复命令结果不确定: %s", exc)
            raise RuntimeError(f"钉钉回复命令结果不确定: {exc}") from exc

        out = stdout.decode("utf-8", "ignore").strip()
        if proc.returncode == 0:
            logger.info("[dingtalk] 已发送回复 -> chat=%s: %s", message.chat_id, message.content[:50])
            return True
        logger.warning("[dingtalk] 回复发送失败(code=%s): %s", proc.returncode, out[:200])
        return False


class DingTalkCliConnector(BaseConnector):
    """通用钉钉 CLI 约定连接器（保留：mode=cli）。

    - settings.command: 拉取新消息命令，标准输出 NDJSON，每行：
      {"chat_id": "...", "sender": "...", "content": "...", "ts": 1700000000000}
    - settings.reply_command: 回复命令模板，占位符 {chat_id} {sender} {content}
    """

    platform = Platform.DINGTALK

    async def poll(self) -> list[ChatMessage]:
        if not self.settings.command:
            return []
        try:
            proc = await asyncio.create_subprocess_shell(
                self.settings.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dingtalk] 启动 CLI 失败: %s", exc)
            return []
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("[dingtalk] CLI 拉取超时: %s", self.settings.command)
            return []
        if proc.returncode != 0:
            logger.warning(
                "[dingtalk] CLI 退出码 %s: %s",
                proc.returncode, stderr.decode("utf-8", "ignore")[:200],
            )
            return []

        out: list[ChatMessage] = []
        for line in stdout.decode("utf-8", "ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("[dingtalk] 无法解析为 JSON 的行: %s", line[:100])
                continue
            external_id = str(data.get("message_id") or data.get("messageId") or "")
            stable_id = "cli_" + hashlib.sha256(f"{data.get('chat_id', 'default')}:{external_id}".encode()).hexdigest()[:24] if external_id else None
            out.append(
                ChatMessage(
                    **({"id": stable_id} if stable_id else {}),
                    platform=Platform.DINGTALK,
                    chat_id=str(data.get("chat_id", "default")),
                    sender=str(data.get("sender", "")),
                    content=str(data.get("content", "")),
                    ts=int(data.get("ts") or now_ms()),
                )
            )
        return out

    async def send_reply(self, message: ChatMessage) -> bool:
        if not self.settings.reply_command:
            logger.warning("[dingtalk] 未配置 reply_command，回复仅记录不发送")
            return False
        cmd = (
            self.settings.reply_command
            .replace("{chat_id}", message.chat_id)
            .replace("{sender}", message.sender)
            .replace("{content}", message.content)
        )
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError as exc:
            proc.kill()  # type: ignore[union-attr]
            logger.warning("[dingtalk] 发送回复结果不确定（超时）")
            raise RuntimeError("钉钉 CLI 回复超时，发送结果不确定") from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dingtalk] 发送回复结果不确定: %s", exc)
            raise RuntimeError(f"钉钉 CLI 回复结果不确定: {exc}") from exc
        if proc.returncode != 0:
            logger.warning(
                "[dingtalk] 发送回复失败(退出码 %s): %s",
                proc.returncode, stderr.decode("utf-8", "ignore")[:200],
            )
            return False
        else:
            logger.info("[dingtalk] 已发送回复 -> chat=%s", message.chat_id)
            return True
