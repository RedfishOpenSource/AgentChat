"""核心调度：监听新消息 → 生成回复草稿 → 人工确认后发送。

安全约束：本服务不自动发送任何消息。回复生成后一律进入待审核（pending）
或拦截（blocked）状态，唯一的发送路径是 approve_reply()（人工确认，可编辑后发送）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Sequence

from ..config import Settings
from ..connectors import BaseConnector, build_connectors
from ..connectors.base import ConnectorStartError
from ..engine import ReplyGenerator, build_engine
from ..engine.jev import JEVJudge
from ..engine.vision import VisionDescriber, resolve_image_source
from ..alerts import AlertManager
from ..models import (
    ChatMessage,
    DIRECTION_OUT,
    Platform,
    STATUS_BLOCKED,
    STATUS_IGNORED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_SENT,
    type_label,
)
from ..rag import KnowledgeBase
from ..store import EventBus, MessageStore

logger = logging.getLogger(__name__)


class AgentService:
    def __init__(
        self,
        settings: Settings,
        store: MessageStore,
        bus: EventBus,
        engine: ReplyGenerator,
        jev: JEVJudge | None = None,
        alerts: AlertManager | None = None,
        kb: KnowledgeBase | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.bus = bus
        self.engine = engine
        self.jev = jev
        self.alerts = alerts or AlertManager()
        self.kb = kb or KnowledgeBase()
        self.connectors: dict[Platform, BaseConnector] = {}
        # 图片理解（可选）：配置了 llm.vision_model 才创建
        self._vision = self._build_vision(settings)
        # LLM 连续失败计数（告警用）
        self._llm_fail_streak = 0
        self._watch_task: asyncio.Task | None = None
        # 连发合并（debounce）状态：按 (platform, chat_id) 维护积压消息与静默计时任务
        self._debounce = max(0.0, float(getattr(settings, "debounce_seconds", 0.0) or 0.0))
        self._pending: dict[tuple[str, str], list[ChatMessage]] = {}
        self._timers: dict[tuple[str, str], asyncio.Task] = {}
        # 限流窗口：每会话的生成时间戳
        self._gen_windows: dict[tuple[str, str], deque[float]] = {}

    # ---------- 连接器管理 ----------
    @staticmethod
    def _build_vision(settings: Settings) -> VisionDescriber | None:
        """按配置创建视觉描述器；未配置 vision_model 时返回 None（图片走占位草稿）。"""
        model = str(getattr(settings.llm, "vision_model", "") or "").strip()
        if not model:
            return None
        base_url = str(settings.llm.vision_base_url or "").strip() or settings.llm.base_url
        api_key = str(settings.llm.vision_api_key or "").strip() or settings.llm.api_key
        if not base_url:
            logger.warning("[vision] 已配置 vision_model 但无 base_url，图片理解未启用")
            return None
        logger.info("[vision] 图片理解已启用: model=%s base=%s", model, base_url)
        return VisionDescriber(base_url=base_url, api_key=api_key, model=model)

    def setup_connectors(self) -> dict[Platform, BaseConnector]:
        self.connectors = build_connectors(self.settings, self._on_messages)
        return self.connectors

    async def start_all(self) -> None:
        """启动所有配置为 auto_start 的连接器（默认全部不自动启动，由前端按钮控制）。"""
        for platform, conn in self.connectors.items():
            if not conn.settings.auto_start:
                continue
            try:
                await conn.start()
            except ConnectorStartError as exc:
                logger.error("连接器 %s 启动失败: %s", platform.value, exc)
                self.alerts.raise_alert(
                    f"connector-{platform.value}", f"{platform.value} 连接器启动失败: {exc}"
                )
        self._publish_status()
        if self._watch_task is None:
            self._watch_task = asyncio.create_task(self._watch_connectors())

    async def stop_all(self) -> None:
        for conn in self.connectors.values():
            if conn.status == "running":
                try:
                    await conn.stop()
                except Exception:  # noqa: BLE001
                    logger.exception("[%s] 停止连接器失败", conn.platform.value)
        self._publish_status()

    async def start_connector(self, platform: Platform) -> BaseConnector:
        conn = self._get_connector(platform)
        await conn.start()
        self._publish_status()
        return conn

    async def stop_connector(self, platform: Platform) -> BaseConnector:
        conn = self._get_connector(platform)
        await conn.stop()
        self._publish_status()
        return conn

    def _get_connector(self, platform: Platform) -> BaseConnector:
        conn = self.connectors.get(platform)
        if conn is None:
            raise KeyError(f"平台 {platform.value} 未启用")
        return conn

    def _publish_status(self) -> None:
        self.bus.publish(
            {"type": "status", "connectors": {p.value: c.info() for p, c in self.connectors.items()}}
        )

    # ---------- 消息管线 ----------
    async def _on_messages(self, msgs: Sequence[ChatMessage]) -> None:
        for m in msgs:
            await self.ingest(m)

    async def ingest(self, msg: ChatMessage) -> ChatMessage:
        """新消息入口：存储 → 广播 → 进入连发合并调度（静默期后触发生成）。"""
        existing = self.store.get(msg.id)
        if existing is not None:  # 重复消息，跳过
            return existing
        stored = self.store.add(msg)
        self.bus.publish({"type": "message", "message": msg.model_dump(mode="json")})
        logger.info("[agent] 新消息 %s(%s/%s): %s", msg.id, msg.platform.value, msg.sender, msg.content[:40])
        if not self._contact_allowed(stored):
            await self._ignore(
                stored, f"不在联系人白名单（chat={stored.chat_id}），可在前台白名单面板添加"
            )
            return stored
        await self._schedule(stored)
        return stored

    def _contact_allowed(self, m: ChatMessage) -> bool:
        """联系人白名单：空列表 = 允许全部；含 "*" = 允许全部；否则按会话名/用户名精确匹配。"""
        allowed = [str(x) for x in (self.settings.allowed_contacts or []) if str(x).strip()]
        if not allowed or "*" in allowed:
            return True
        candidates = {m.chat_id}
        meta = m.meta or {}
        for section in ("dws", "replica"):
            section_meta = meta.get(section) or {}
            for k in ("title", "username"):
                v = section_meta.get(k)
                if v:
                    candidates.add(str(v))
        return bool(candidates & set(allowed))

    # ---------- 连发合并（debounce） ----------
    def _key(self, m: ChatMessage) -> tuple[str, str]:
        return (m.platform.value, m.chat_id)

    async def _schedule(self, msg: ChatMessage) -> None:
        key = self._key(msg)
        self._pending.setdefault(key, []).append(msg)
        old = self._timers.get(key)
        if old is not None and not old.done():
            old.cancel()  # 同会话有新消息 → 重置静默计时
        if self._debounce <= 0:
            self._timers.pop(key, None)
            await self._flush(key)  # 关闭合并：保持同步语义（响应内完成生成）
        else:
            self._timers[key] = asyncio.create_task(self._debounce_wait(key))

    async def _debounce_wait(self, key: tuple[str, str]) -> None:
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return
        self._timers.pop(key, None)
        await self._flush(key)

    async def _flush(self, key: tuple[str, str]) -> None:
        """静默期满：把该会话积压的连发消息合并为一次生成（取最后一条为触发消息）。"""
        msgs = self._pending.pop(key, [])
        if not msgs:
            return
        primary = msgs[-1]
        logger.info("[agent] 静默期结束，合并 %d 条连发消息生成回复（触发: %s）", len(msgs), primary.id)
        await self._generate_and_reply(primary)

    async def _generate_and_reply(self, incoming: ChatMessage) -> None:
        """需求(1)：监听到新消息后生成回复（全部消息场景显式处理）。"""
        # ---- 场景策略过滤 ----
        skip_reason = self._policy_skip_reason(incoming)
        if skip_reason:
            await self._ignore(incoming, skip_reason)
            return

        # ---- 非文本消息 ----
        if incoming.msg_type != "text":
            if self.settings.non_text_action == "skip":
                await self._ignore(incoming, f"非文本消息({incoming.msg_type})，策略为忽略")
                return
            await self._handle_non_text(incoming)
            return

        # ---- 限流检查（仅限制引擎生成；非文本占位不占用额度） ----
        limit_reason = self._rate_limit_check(incoming)
        if limit_reason:
            await self._ignore(incoming, limit_reason)
            return

        # ---- 文本消息：正常引擎生成 ----
        history = [
            h for h in self.store.history(incoming.platform, incoming.chat_id,
                                          limit=self.settings.llm.max_history)
            if h.id != incoming.id
        ]
        try:
            result = await self.engine.generate(incoming, history)
        except Exception as exc:  # noqa: BLE001
            # 降级：引擎不可用时生成错误占位草稿，进入人工流程（消息绝不静默丢失）
            self._record_llm_outcome(False, str(exc))
            logger.exception("生成回复失败: %s", exc)
            reply = ChatMessage(
                platform=incoming.platform,
                chat_id=incoming.chat_id,
                sender="assistant",
                content="⚠️ AI 引擎暂时不可用，请人工回复这条消息",
                direction=DIRECTION_OUT,
                status=STATUS_PENDING,
                reply_to=incoming.id,
                meta={"engine": "error", "error": str(exc)[:200]},
            )
            self._apply_reply_routing(reply, incoming)
            self.store.add(reply)
            self.bus.publish({"type": "reply", "message": reply.model_dump(mode="json")})
            return
        self._record_llm_outcome(True)

        # 安全约束：blocked 永不自动发送；auto_send=True 时普通草稿直接发送（前端按钮可切）
        if result.blocked:
            status = STATUS_BLOCKED
        elif self.settings.auto_send:
            status = STATUS_SENT
        else:
            status = STATUS_PENDING

        reply = ChatMessage(
            platform=incoming.platform,
            chat_id=incoming.chat_id,
            sender="assistant",
            content=result.content,
            direction=DIRECTION_OUT,
            status=status,
            reply_to=incoming.id,
            meta=result.meta,
        )
        # 透传连接器发送路由元数据：钉钉 dws 引用回复 / 微信群聊引用+文本@
        self._apply_reply_routing(reply, incoming)
        self.store.add(reply)
        self.bus.publish({"type": "reply", "message": reply.model_dump(mode="json")})
        logger.info(
            "[agent] 已生成回复 %s -> %s [%s]%s",
            reply.id, incoming.id, status,
            "（已自动发送）" if status == STATUS_SENT else "（等待人工确认）",
        )
        if status == STATUS_SENT:  # 仅普通草稿；blocked 永不自动发送
            await self._dispatch(reply)

    async def _handle_non_text(self, incoming: ChatMessage) -> None:
        """非文本消息：图片且有可用视觉模型时生成 AI 描述草稿，否则生成占位草稿。

        两者都只是待确认草稿（不走自动发送），由人工决定是否回复及回复内容。
        """
        label = type_label(incoming.msg_type)
        content = f"[收到{label}消息，暂不支持内容识别，请人工处理]"
        meta: dict = {"engine": "system", "placeholder": True, "msg_type": incoming.msg_type}

        if incoming.msg_type == "image" and self._vision is not None:
            source = resolve_image_source(incoming.meta)
            if not source:
                logger.info("[vision] 图片消息无 image_path/image_url 元数据，使用占位草稿")
            else:
                try:
                    desc = await self._vision.describe(source)
                    content = f"[图片] AI 识别：{desc}"
                    meta = {"engine": "vision", "vision_model": self._vision.model,
                            "image_source": source, "placeholder": False,
                            "msg_type": incoming.msg_type}
                    logger.info("[vision] 图片描述完成 %s: %s", incoming.id, desc[:50])
                except Exception as exc:  # noqa: BLE001 —— 识别失败兜底为占位草稿
                    logger.warning("[vision] 图片识别失败，兜底占位草稿: %s", exc)
                    meta["vision_error"] = str(exc)[:200]

        reply = ChatMessage(
            platform=incoming.platform,
            chat_id=incoming.chat_id,
            sender="assistant",
            content=content,
            direction=DIRECTION_OUT,
            status=STATUS_PENDING,
            reply_to=incoming.id,
            meta=meta,
        )
        self._apply_reply_routing(reply, incoming)
        self.store.add(reply)
        self.bus.publish({"type": "reply", "message": reply.model_dump(mode="json")})
        logger.info("[agent] 非文本消息(%s)生成草稿 %s", incoming.msg_type, reply.id)

    def _wechat_group_reply_meta(self, incoming: ChatMessage) -> dict | None:
        """微信群聊 quote-at 回复元数据；非微信/非群聊/已关闭时返回 None。

        meta 供 replica 连接器发送时走 quote_msg（引用原消息）；
        at_sender 同时用于给草稿正文拼「@昵称 」前缀（WYSIWYG，批准即所见）。
        """
        if incoming.platform != Platform.WECHAT or not incoming.is_group:
            return None
        if getattr(self.settings.wechat, "group_reply", "quote-at") != "quote-at":
            return None
        sender = (incoming.sender or "").strip()
        quote_text = incoming.content.strip()[:20]  # OCR 定位引用目标的短文案
        if not sender or not quote_text:
            return None
        return {"at_sender": sender, "quote_text": quote_text}

    def _apply_reply_routing(self, reply: ChatMessage, incoming: ChatMessage) -> None:
        """透传连接器发送路由所需元数据。

        - 钉钉 dws：message_id/conversation_id，供引用回复使用
        - 微信群聊（quote-at）：附 wechat_reply 元数据，并把「@触发者 」前缀
          写进草稿正文（前端可见可编辑；切换候选时由 _ensure_group_prefix 补回）
        """
        dws_src = (incoming.meta or {}).get("dws")
        if dws_src:
            reply.meta = {**reply.meta, "dws": dws_src}
        wr = self._wechat_group_reply_meta(incoming)
        if wr:
            reply.meta = {**reply.meta, "wechat_reply": wr}
            reply.content = self._ensure_group_prefix(reply, reply.content)

    @staticmethod
    def _ensure_group_prefix(reply: ChatMessage, content: str) -> str:
        """给微信群聊回复补「@昵称 」前缀（已有则不动；候选原文不含前缀）。"""
        wr = (reply.meta or {}).get("wechat_reply") or {}
        sender = str(wr.get("at_sender") or "").strip()
        if not sender:
            return content
        prefix = f"@{sender} "
        return content if content.startswith(prefix) else prefix + content

    def _rate_limit_check(self, m: ChatMessage) -> str | None:
        """每会话每分钟生成次数限制；返回拒绝原因或 None。"""
        limit = int(getattr(self.settings, "llm_rate_limit_per_chat", 0) or 0)
        if limit <= 0:
            return None
        key = self._key(m)
        now = time.monotonic()
        dq = self._gen_windows.setdefault(key, deque())
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= limit:
            return f"触发频率限制（每会话每分钟最多 {limit} 次生成），请人工回复"
        dq.append(now)
        return None

    # ---------- 配置热更新 ----------
    async def apply_config(self, updates: dict) -> dict:
        """热更新配置（子集），返回变更清单。不支持的项抛 ValueError。"""
        s = self.settings
        changed: list[str] = []
        rebuild = False

        engine = updates.get("engine")
        if engine is not None:
            if engine not in ("mock", "llm"):
                raise ValueError("engine 仅支持 mock / llm")
            if engine != s.engine:
                s.engine = engine
                rebuild = True
                changed.append("engine")

        if updates.get("jev_enabled") is not None:
            s.jev.enabled = bool(updates["jev_enabled"])
            if self.jev is not None:
                self.jev.enabled = s.jev.enabled
            changed.append("jev_enabled")
        if updates.get("jev_provider") is not None:
            if updates["jev_provider"] not in ("mock", "typesafe"):
                raise ValueError("jev_provider 仅支持 mock / typesafe")
            s.jev.provider = updates["jev_provider"]
            rebuild = True
            changed.append("jev_provider")
        if updates.get("jev_api_key"):
            s.jev.api_key = str(updates["jev_api_key"])
            rebuild = True
            changed.append("jev_api_key")

        if updates.get("num_candidates") is not None:
            nc = int(updates["num_candidates"])
            if not 1 <= nc <= 5:
                raise ValueError("num_candidates 范围 1-5")
            s.llm.num_candidates = nc  # 生成器持有同一 cfg 引用，热生效
            changed.append("num_candidates")
        if updates.get("temperature") is not None:
            s.llm.temperature = min(2.0, max(0.0, float(updates["temperature"])))
            changed.append("temperature")
        if updates.get("llm_max_retries") is not None:
            s.llm.max_retries = max(0, int(updates["llm_max_retries"]))
            changed.append("llm_max_retries")
        if updates.get("llm_style") is not None:
            from ..engine.llm import STYLE_PRESETS  # noqa: PLC0415 —— 避免模块级循环依赖
            if updates["llm_style"] not in STYLE_PRESETS:
                raise ValueError(f"llm_style 仅支持 {' / '.join(STYLE_PRESETS)}")
            s.llm.style = updates["llm_style"]  # 生成器持有同一 cfg 引用，热生效
            changed.append("llm_style")
        if updates.get("knowledge_dir") is not None:
            path = str(updates["knowledge_dir"]).strip()
            if path:
                self.kb.configure(path)  # 目录不存在抛 ValueError → 400
            else:
                self.kb.reset()
            s.knowledge_dir = path
            changed.append("knowledge_dir")
        if updates.get("embedding_enabled") is not None:
            s.embedding.enabled = bool(updates["embedding_enabled"])
            try:
                self.kb.set_embedding(
                    s.embedding,
                    llm_base_url=s.llm.base_url,
                    llm_api_key=s.llm.api_key,
                    cache_path=self._embedding_cache_path(),
                )
                changed.append("embedding_enabled")
            except ValueError as exc:
                s.embedding.enabled = not s.embedding.enabled  # 回滚，保持配置一致
                raise ValueError(f"embedding 配置无效: {exc}") from exc

        if updates.get("group_trigger") is not None:
            if updates["group_trigger"] not in ("at-mention", "all", "listen-only"):
                raise ValueError("group_trigger 仅支持 at-mention / all / listen-only")
            s.group_trigger = updates["group_trigger"]
            changed.append("group_trigger")
        if updates.get("non_text_action") is not None:
            if updates["non_text_action"] not in ("placeholder", "skip"):
                raise ValueError("non_text_action 仅支持 placeholder / skip")
            s.non_text_action = updates["non_text_action"]
            changed.append("non_text_action")
        if updates.get("debounce_seconds") is not None:
            s.debounce_seconds = max(0.0, float(updates["debounce_seconds"]))
            self._debounce = s.debounce_seconds
            changed.append("debounce_seconds")
        if updates.get("llm_rate_limit_per_chat") is not None:
            s.llm_rate_limit_per_chat = max(0, int(updates["llm_rate_limit_per_chat"]))
            changed.append("llm_rate_limit_per_chat")

        if updates.get("alert_channel") is not None:
            s.alert_channel = str(updates["alert_channel"])
            changed.append("alert_channel")
        if updates.get("alert_dingtalk_chat_id") is not None:
            s.alert_dingtalk_chat_id = str(updates["alert_dingtalk_chat_id"])
            changed.append("alert_dingtalk_chat_id")
        if "alert_channel" in changed or "alert_dingtalk_chat_id" in changed:
            from ..alerts import build_notifier  # noqa: PLC0415
            self.alerts.set_notifier(
                build_notifier(s.alert_channel, s.alert_dingtalk_chat_id, dws_bin=s.dingtalk.dws_bin)
            )

        if updates.get("allowed_contacts") is not None:
            s.allowed_contacts = [str(x).strip() for x in updates["allowed_contacts"] if str(x).strip()]
            changed.append("allowed_contacts")
        if updates.get("auto_send") is not None:
            s.auto_send = bool(updates["auto_send"])
            changed.append("auto_send")

        if rebuild:
            old_engine, old_jev = self.engine, self.jev
            self.engine, self.jev = build_engine(s, kb=self.kb)
            for closer in (old_engine, old_jev):
                if closer is not None:
                    try:
                        await closer.aclose()
                    except Exception:  # noqa: BLE001
                        pass
            changed.append("engine_rebuilt")
        return {"changed": changed}

    # ---------- 监控告警 ----------
    async def _watch_connectors(self) -> None:
        """连接器健康巡检：error 状态激活告警，恢复后解除。"""
        interval = max(0.1, float(getattr(self.settings, "alert_check_interval", 30.0)))
        try:
            while True:
                for platform, conn in self.connectors.items():
                    key = f"connector-{platform.value}"
                    if conn.status == "error":
                        self.alerts.raise_alert(
                            key, f"{platform.value} 连接器异常: {conn.last_error or '未知原因'}"
                        )
                    else:
                        self.alerts.resolve_alert(key)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    def _record_llm_outcome(self, ok: bool, error: str = "") -> None:
        threshold = max(1, int(getattr(self.settings, "llm_failure_threshold", 3)))
        if ok:
            if self._llm_fail_streak > 0:
                self._llm_fail_streak = 0
            self.alerts.resolve_alert("llm-failure")
            return
        self._llm_fail_streak += 1
        if self._llm_fail_streak >= threshold:
            self.alerts.raise_alert(
                "llm-failure",
                f"LLM 连续失败 {self._llm_fail_streak} 次（阈值 {threshold}）: {error[:120]}",
            )

    # ---------- 消息场景策略 ----------
    def _policy_skip_reason(self, m: ChatMessage) -> str | None:
        """返回需要忽略的原因；None 表示正常处理。"""
        if m.msg_type == "system":
            return "系统消息不生成回复"
        if m.is_group and self.settings.group_trigger == "listen-only":
            return "群聊策略为只监听不回复"
        if m.is_group and self.settings.group_trigger == "at-mention" and not m.at_me:
            return "群聊消息未@我（策略：仅回复@我）"
        return None

    async def _ignore(self, m: ChatMessage, reason: str) -> None:
        """按策略忽略：标记状态并广播，不生成草稿。"""
        m.status = STATUS_IGNORED
        m.meta = {**m.meta, "ignore_reason": reason}
        self.store.update(m)
        self.bus.publish({"type": "message_updated", "message": m.model_dump(mode="json")})
        logger.info("[agent] 消息已忽略(%s): %s", reason, m.id)

    async def _dispatch(self, reply: ChatMessage) -> None:
        """发送回复到平台。仅允许在 approve_reply() 人工确认后被调用。"""
        conn = self.connectors.get(reply.platform)
        if conn is None:
            logger.warning("平台 %s 无可用连接器，回复仅记录不发送", reply.platform)
            return
        await conn.send_reply(reply)

    # ---------- 人工审核（唯一的发送路径） ----------
    async def approve_reply(self, reply_id: str, content: str | None = None) -> ChatMessage:
        """人工确认发送。

        content: 可选的草稿编辑结果；提供且非空则发送编辑后的内容（原稿保留在 meta.edited_from）。
        """
        reply = self.store.get(reply_id)
        if reply is None or reply.direction != DIRECTION_OUT:
            raise KeyError(reply_id)
        if reply.status not in (STATUS_PENDING, STATUS_BLOCKED):
            raise ValueError(f"回复当前状态为 {reply.status}，不能发送")
        if content is not None and content.strip() and content != reply.content:
            original = reply.content
            reply.content = content.strip()
            reply.meta = {**reply.meta, "edited_from": original}
        reply.status = STATUS_SENT
        self.store.update(reply)
        self.store.audit(
            "admin", "approve", reply.id,
            json.dumps({"content": reply.content, "edited_from": reply.meta.get("edited_from")},
                       ensure_ascii=False),
        )
        await self._dispatch(reply)
        self.bus.publish({"type": "reply_updated", "message": reply.model_dump(mode="json")})
        return reply

    async def select_candidate(self, reply_id: str, index: int) -> ChatMessage:
        """多候选切换：把第 index 条候选设为草稿正文（原内容保留在 edited_from）。"""
        reply = self.store.get(reply_id)
        if reply is None or reply.direction != DIRECTION_OUT:
            raise KeyError(reply_id)
        if reply.status not in (STATUS_PENDING, STATUS_BLOCKED):
            raise ValueError(f"回复当前状态为 {reply.status}，不能切换候选")
        cands = (reply.meta or {}).get("candidates") or []
        if not (0 <= index < len(cands)):
            raise ValueError(f"候选序号超出范围（0..{len(cands) - 1}）")
        content = str(cands[index].get("content") or "").strip()
        if not content:
            raise ValueError("该候选内容为空")
        content = self._ensure_group_prefix(reply, content)
        if content != reply.content:
            reply.meta = {**reply.meta, "edited_from": reply.content}
        reply.content = content
        reply.meta = {**reply.meta, "selected_candidate": index}
        self.store.update(reply)
        self.bus.publish({"type": "reply_updated", "message": reply.model_dump(mode="json")})
        return reply

    async def reject_reply(self, reply_id: str) -> ChatMessage:
        reply = self.store.get(reply_id)
        if reply is None or reply.direction != DIRECTION_OUT:
            raise KeyError(reply_id)
        if reply.status not in (STATUS_PENDING, STATUS_BLOCKED):
            raise ValueError(f"回复当前状态为 {reply.status}，不能驳回")
        reply.status = STATUS_REJECTED
        self.store.update(reply)
        self.store.audit("admin", "reject", reply.id, reply.content[:100])
        self.bus.publish({"type": "reply_updated", "message": reply.model_dump(mode="json")})
        return reply

    # ---------- 直接对话（前端“对话测试”，走同一个生成器接口） ----------
    async def direct_chat(self, content: str, chat_id: str = "web-demo") -> tuple[ChatMessage, ChatMessage]:
        user_msg = ChatMessage(platform=Platform.WEB, chat_id=chat_id, sender="user", content=content)
        self.store.add(user_msg)
        self.bus.publish({"type": "message", "message": user_msg.model_dump(mode="json")})
        history = [
            h for h in self.store.history(Platform.WEB, chat_id, limit=self.settings.llm.max_history)
            if h.id != user_msg.id
        ]
        result = await self.engine.generate(user_msg, history)
        reply = ChatMessage(
            platform=Platform.WEB,
            chat_id=chat_id,
            sender="assistant",
            content=result.content,
            direction=DIRECTION_OUT,
            status=STATUS_SENT,
            reply_to=user_msg.id,
            meta=result.meta,
        )
        self.store.add(reply)
        self.bus.publish({"type": "reply", "message": reply.model_dump(mode="json")})
        return user_msg, reply

    # ---------- 退出 ----------
    def _embedding_cache_path(self) -> str:
        """向量缓存文件路径：与数据库同目录（db 为 :memory: 时用当前目录）。"""
        db = Path(self.settings.db_path)
        return str(db.parent / "rag_embeddings.json") if str(db.parent) != "." else "rag_embeddings.json"

    async def shutdown(self) -> None:
        # 尽力冲刷未处理的连发积压，避免丢草稿（消息已落盘，草稿生成后也会落盘）
        for key in list(self._pending.keys()):
            try:
                await self._flush(key)
            except Exception:  # noqa: BLE001
                logger.exception("[agent] 关闭时冲刷积压消息失败: %s", key)
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        if self._watch_task is not None:
            self._watch_task.cancel()
            self._watch_task = None
        await self.stop_all()
        try:
            await self.engine.aclose()
        except Exception:  # noqa: BLE001
            pass
        if self.jev is not None:
            try:
                await self.jev.aclose()
            except Exception:  # noqa: BLE001
                pass
        if self._vision is not None:
            try:
                await self._vision.aclose()
            except Exception:  # noqa: BLE001
                pass
        try:
            await self.kb.aclose()
        except Exception:  # noqa: BLE001
            pass
