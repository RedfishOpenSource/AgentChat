"""核心调度：监听新消息 → 生成回复草稿 → 人工确认后发送。

安全约束：本服务不自动发送任何消息。回复生成后一律进入待审核（pending）
或拦截（blocked）状态，唯一的发送路径是 approve_reply()（人工确认，可编辑后发送）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit
from typing import Sequence

from ..agents_registry import find_agent
from ..config import ACPSettings, Settings
from ..mcp_config import load_workspace_mcps
from ..connectors import BaseConnector, build_connectors
from ..connectors.base import ConnectorStartError
from ..engine import ACPAgentGenerator, ReplyGenerator, build_engine
from ..engine.vision import VisionDescriber, resolve_image_source
from ..engine.output import clean_agent_reply
from ..alerts import AlertManager
from ..models import (
    ChatMessage,
    DIRECTION_OUT,
    Platform,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_IGNORED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_SENT,
    STATUS_SENDING,
    STATUS_UNKNOWN,
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
        alerts: AlertManager | None = None,
        kb: KnowledgeBase | None = None,
        db=None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.bus = bus
        self.engine = engine
        self.alerts = alerts or AlertManager()
        self.kb = kb or KnowledgeBase()
        self.db = db
        self.agent_profiles = {p["id"]: p for p in (db.list_agent_profiles() if db else [])}
        self.agent_assignments = {
            (item["platform"], item["chat_id"]): item["agent_id"]
            for item in (db.list_agent_assignments() if db else [])
        }
        self.profile_engines: dict[str, ACPAgentGenerator] = {}
        self.profile_tasks: dict[str, asyncio.Task] = {}
        for profile in self.agent_profiles.values():
            if profile.get("pending"):
                profile.update(pending=False, rewrite_status="failed", rewrite_error="上次角色改写因服务重启中断，请重新保存")
                if db:
                    db.save_agent_profile(profile)
        self.session_resets = {
            (r["platform"], r["chat_id"]): r for r in (db.list_session_resets() if db else [])
        }
        self.contact_configs: dict[tuple[str, str], dict] = {
            k: v for k, v in (db.list_contact_configs().items() if db else {})
        }
        self.prune_removed_contacts()
        # 每个连接器可配置独立知识库（connector.knowledge_dir）；未配置时回退全局 self.kb
        self.kbs: dict[Platform, KnowledgeBase] = {}
        for platform in (Platform.WECHAT, Platform.DINGTALK):
            self._rebuild_platform_kb(platform, log_errors=False)
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
        # 思考状态（合并中/生成中）：按会话广播，供前端展示进度
        self._thinking: dict[str, dict] = {}
        self._progress_published: dict[str, float] = {}
        self._recover_task: asyncio.Task | None = None

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
        """恢复未完成的消息处理，再启动配置为 auto_start 的连接器。"""
        if self._recover_task is None:
            self._recover_task = asyncio.create_task(self._recover_unprocessed())
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

    async def _recover_unprocessed(self) -> None:
        """重新调度已持久化但没有回复的收到消息；投递中的回复由 store 转为 unknown。"""
        incoming_items = self.store.list_messages(direction="in", limit=1000)
        replies = self.store.list_messages(direction="out", limit=1000)
        replied_to = {m.reply_to for m in replies if m.reply_to}
        for incoming in incoming_items:
            if incoming.status == "received" and incoming.id not in replied_to:
                await self._schedule(incoming)

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

    async def regenerate_with_supplement(self, origin: ChatMessage, extra: str) -> ChatMessage:
        """对某条收到的消息补充说明后重新生成：补充作为新消息入链，再走正常生成管线。"""
        supplement = ChatMessage(
            platform=origin.platform,
            chat_id=origin.chat_id,
            sender="操作员",
            content=extra,
            meta={"supplement_to": origin.id, "operator_input": True,
                  **({"dws": origin.meta["dws"]} if origin.meta.get("dws") else {})},
        )
        self._tag_session(supplement)
        self.store.add(supplement)
        self.bus.publish({"type": "message", "message": supplement.model_dump(mode="json")})
        await self._generate_and_reply(supplement)
        replies = [
            m for m in self.store.list_messages(chat_id=origin.chat_id, direction="out")
            if m.reply_to == supplement.id
        ]
        return replies[-1] if replies else supplement

    async def contact_chat(self, platform: Platform, chat_id: str, content: str) -> tuple[ChatMessage, ChatMessage | None]:
        """操作员补充当前对话：复用联系人会话，始终只生成待审核草稿。"""
        origin = next((m for m in reversed(self.store.list_messages(
            platform=platform.value, chat_id=chat_id, direction="in",
        )) if not m.meta.get("operator_input")), None)
        meta = {"operator_input": True}
        if origin and origin.meta.get("dws"):
            meta["dws"] = origin.meta["dws"]
        incoming = ChatMessage(
            platform=platform, chat_id=chat_id, sender="操作员", content=content, meta=meta,
        )
        self._tag_session(incoming)
        self.store.add(incoming)
        self.bus.publish({"type": "message", "message": incoming.model_dump(mode="json")})
        await self._generate_and_reply(incoming)
        reply = next((m for m in self.store.list_messages(
            platform=platform.value, chat_id=chat_id, direction="out",
        ) if m.reply_to == incoming.id), None)
        return incoming, reply

    async def debug_chat(self, profile: dict, session_id: str, content: str) -> tuple[ChatMessage, ChatMessage | None]:
        """直接调试指定 Agent；使用独立 web 会话，禁止平台投递。"""
        incoming = ChatMessage(
            platform=Platform.WEB, chat_id=f"debug:{profile['id']}:{session_id}",
            sender="开发者", content=content,
            meta={"operator_input": True, "debug_profile_id": profile["id"]},
        )
        self.store.add(incoming)
        self.bus.publish({"type": "message", "message": incoming.model_dump(mode="json")})
        await self._generate_and_reply(incoming, profile=profile)
        reply = next((m for m in self.store.list_messages(
            platform="web", chat_id=incoming.chat_id, direction="out",
        ) if m.reply_to == incoming.id), None)
        return incoming, reply

    def contact_processing(self, platform: Platform, chat_id: str) -> bool:
        return bool(self._pending.get((platform.value, chat_id))) or any(
            state["platform"] == platform.value and state["chat_id"] == chat_id
            for state in self._thinking.values()
        )

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
        self._tag_session(msg)
        stored = self.store.add(msg)
        self.bus.publish({"type": "message", "message": msg.model_dump(mode="json")})
        logger.info("[agent] 新消息 %s(%s/%s): %s", msg.id, msg.platform.value, msg.sender, msg.content[:40])
        if not self._contact_allowed(stored):
            await self._ignore(
                stored, f"未选择监听该联系人（chat={stored.chat_id}），可在「监听联系人」中添加"
            )
            return stored
        await self._schedule(stored)
        return stored

    def reset_contact_session(self, platform: Platform, chat_id: str) -> int:
        """清空并删除该联系人的全部聊天记录（页面与数据库），开启全新 Agent 会话。"""
        if self.contact_processing(platform, chat_id):
            raise ValueError("该对话正在处理中，请等待完成后再清空会话")
        key = (platform.value, chat_id)
        token, reset_at = uuid.uuid4().hex, int(time.time() * 1000)
        if self.db:
            self.db.save_session_reset(platform.value, chat_id, token, reset_at)
        self.session_resets[key] = {"platform": platform.value, "chat_id": chat_id,
                                    "token": token, "reset_at": reset_at}
        for engine in [self.engine, *self.profile_engines.values()]:
            fn = getattr(engine, "reset_session", None)
            if fn is not None:
                fn(platform.value, chat_id)
            self._apply_session_token(engine, platform, chat_id)
        deleted = self.store.remove_chat(platform, chat_id)
        self.bus.publish({"type": "chat_cleared", "platform": platform.value,
                          "chat_id": chat_id, "deleted": deleted})
        return deleted

    def _apply_session_token(self, engine, platform: Platform, chat_id: str) -> None:
        boundary = self.session_resets.get((platform.value, chat_id))
        setter = getattr(engine, "set_session_token", None)
        if boundary and setter:
            setter(platform.value, chat_id, boundary["token"])

    def _session_history(self, incoming: ChatMessage) -> list[ChatMessage]:
        boundary = self.session_resets.get(self._key(incoming))
        return [h for h in self.store.history(incoming.platform, incoming.chat_id,
                                             limit=self.settings.llm.max_history)
                if h.id != incoming.id and not h.meta.get("session_reset")
                and (not boundary or h.meta.get("session_token") == boundary["token"])]

    def _tag_session(self, incoming: ChatMessage) -> None:
        boundary = self.session_resets.get(self._key(incoming))
        if boundary:
            incoming.meta = {**incoming.meta, "session_token": boundary["token"]}

    def agent_assignments_list(self) -> list[dict[str, str]]:
        """联系人绑定清单（platform/chat_id/agent_id），供前端展示。"""
        return [
            {"platform": platform, "chat_id": chat_id, "agent_id": agent_id}
            for (platform, chat_id), agent_id in self.agent_assignments.items()
        ]

    def profile_for(self, platform: Platform, chat_id: str) -> dict | None:
        """联系人绑定的 Agent 配置；未绑定时回退全局默认引擎。"""
        agent_id = self.agent_assignments.get((platform.value, chat_id))
        return self.agent_profiles.get(agent_id) if agent_id else None

    def engine_for_profile(self, profile: dict) -> ACPAgentGenerator:
        """每个 Agent 配置一个独立 ACP 引擎实例（会话互相隔离）。"""
        if profile.get("pending"):
            raise ValueError("Agent 正在重新改写角色，请完成后再使用")
        if profile.get("rewrite_status") == "failed":
            raise ValueError(profile.get("rewrite_error") or "Agent 角色改写失败，请重新保存")
        engine = self.profile_engines.get(profile["id"])
        if engine is not None:
            return engine
        preset = find_agent(str(profile.get("engine") or ""))
        command = str(profile.get("command") or "")
        mode = (preset or {}).get("mode", "acp")
        if profile.get("engine") == "pi" and Path(command).stem.lower() == "pi":
            mode = "pi"  # 兼容手动配置的 pi RPC；pi-acp 使用标准 ACP
        cfg = ACPSettings(
            mode=mode,
            command=str(profile.get("command") or ""),
            args=str(profile.get("args") or ""),
            cwd=str(profile.get("workspace") or ""),
            timeout=float(profile.get("timeout") or 300),
            instructions=str(profile.get("instructions") or ""),
            skills=list(profile.get("skills") or []),
            capabilities=list(profile.get("capabilities") or []),
            knowledge_dir=str(profile.get("knowledge_dir") or ""),
            mcp_servers=load_workspace_mcps(str(profile.get("workspace") or ""), list(profile.get("mcp_servers") or [])),
            plan_before_execute=bool(profile.get("plan_before_execute")),
        )
        engine = ACPAgentGenerator(cfg)
        self.profile_engines[profile["id"]] = engine
        return engine

    def save_agent_profile(self, profile: dict) -> None:
        """保存/更新 Agent 配置并重建其运行时引擎。"""
        if self.db is not None:
            self.db.save_agent_profile(profile)
        self.agent_profiles[profile["id"]] = profile
        self._discard_profile_engine(profile["id"])

    def delete_agent_profile(self, agent_id: str) -> None:
        """删除 Agent 配置，并解除全部联系人绑定。"""
        task = self.profile_tasks.pop(agent_id, None)
        if task:
            task.cancel()
        if self.db is not None:
            self.db.delete_agent_profile(agent_id)
        self.agent_profiles.pop(agent_id, None)
        self.agent_assignments = {
            key: aid for key, aid in self.agent_assignments.items() if aid != agent_id
        }
        self._discard_profile_engine(agent_id)

    def assign_agent(self, platform: str, chat_id: str, agent_id: str | None) -> None:
        """绑定/解绑联系人的 Agent；agent_id=None 表示使用默认引擎。"""
        key = (platform, chat_id)
        if agent_id is None:
            if self.db is not None:
                self.db.unassign_agent(platform, chat_id)
            self.agent_assignments.pop(key, None)
            return
        if self.db is not None:
            self.db.assign_agent(platform, chat_id, agent_id)
        self.agent_assignments[key] = agent_id

    def _discard_profile_engine(self, agent_id: str) -> None:
        engine = self.profile_engines.pop(agent_id, None)
        if engine is None:
            return
        async def _close():
            try:
                await engine.aclose()
            except Exception:  # noqa: BLE001
                pass
        try:
            asyncio.get_running_loop().create_task(_close())
        except RuntimeError:  # pragma: no cover - 同步测试环境无事件循环
            pass

    def contact_config_for(self, platform: Platform, chat_id: str) -> dict:
        """联系人监听配置；未单独设置时回退全局默认。"""
        cfg = self.contact_configs.get((platform.value, chat_id)) or {}
        s = self.settings
        return {
            "timeout": float(cfg.get("timeout") or s.acp.timeout),
            "group_trigger": str(cfg.get("group_trigger") or s.group_trigger),
            "non_text_action": str(cfg.get("non_text_action") or s.non_text_action),
            "debounce_seconds": max(0.0, float(cfg.get("debounce_seconds") if cfg.get("debounce_seconds") is not None else s.debounce_seconds)),
        }

    def save_contact_config(self, platform: str, chat_id: str, config: dict) -> None:
        """保存联系人监听配置（未选中监听的联系人应删除配置）。"""
        if self.db is not None:
            self.db.save_contact_config(platform, chat_id, config)
        self.contact_configs[(platform, chat_id)] = dict(config)

    def delete_contact_config(self, platform: str, chat_id: str) -> None:
        if self.db is not None:
            self.db.delete_contact_config(platform, chat_id)
        self.contact_configs.pop((platform, chat_id), None)

    def prune_removed_contacts(self) -> None:
        """删除已移出监听名单的绑定及配置，也修复旧版本遗留的数据。"""
        for platform in (Platform.WECHAT, Platform.DINGTALK):
            allowed = set(self.effective_contacts(platform))
            if "*" in allowed:
                continue
            keys = set(self.agent_assignments) | set(self.contact_configs)
            for p, chat_id in keys:
                if p == platform.value and chat_id not in allowed:
                    if self.db is not None:
                        self.db.remove_contact(p, chat_id)
                    self.agent_assignments.pop((p, chat_id), None)
                    self.contact_configs.pop((p, chat_id), None)

    def effective_contacts(self, platform: Platform) -> list[str]:
        """平台生效白名单：连接器独立名单优先，未配置时回退全局名单（空 = 允许全部）。"""
        cs = getattr(self.settings, platform.value, None)
        own = [str(x).strip() for x in (getattr(cs, "allowed_contacts", None) or []) if str(x).strip()]
        if own:
            return own
        return [str(x).strip() for x in (self.settings.allowed_contacts or []) if str(x).strip()]

    def kb_for(self, platform: Platform) -> KnowledgeBase:
        """平台生效知识库：连接器独立目录优先，未配置时回退全局知识库。"""
        return self.kbs.get(platform) or self.kb

    def _rebuild_platform_kb(self, platform: Platform, log_errors: bool = True) -> None:
        """按连接器配置重建平台知识库：有独立目录则建专属实例，否则回退全局库。"""
        cs = getattr(self.settings, platform.value, None)
        path = str(getattr(cs, "knowledge_dir", "") or "").strip()
        if not path:
            self.kbs.pop(platform, None)
            return
        try:
            kb = KnowledgeBase()
            kb.configure(path)  # 目录不存在抛 ValueError
            self._apply_embedding(kb, platform)
            self.kbs[platform] = kb
        except ValueError as exc:
            self.kbs.pop(platform, None)
            if log_errors:
                logger.warning("[%s] 知识库目录不可用，回退全局知识库: %s", platform.value, exc)

    def _apply_embedding(self, kb: KnowledgeBase, platform: Platform | None = None) -> None:
        """把全局 embedding 配置应用到指定知识库（未启用时不动作）。"""
        if not self.settings.embedding.enabled:
            return
        try:
            kb.set_embedding(
                self.settings.embedding,
                llm_base_url=self.settings.llm.base_url,
                llm_api_key=self.settings.llm.api_key,
                cache_path=self._embedding_cache_path(platform),
            )
        except ValueError as exc:
            logger.warning("[%s] embedding 配置无效，仅词面检索: %s", platform.value if platform else "global", exc)

    def _contact_allowed(self, m: ChatMessage) -> bool:
        """监听白名单：默认不监听任何人，只有选中的联系人才会监听；"*" = 显式允许全部。

        弹窗保存的是显示名（微信备注/昵称、钉钉群名），而消息 meta 里只有 username/cid；
        因此匹配前先经连接器把显示名反解为 username，一并纳入比对。
        """
        allowed = self.effective_contacts(m.platform)
        if not allowed:
            return False
        if "*" in allowed:
            return True
        allowed_set = {str(x).strip() for x in allowed if str(x).strip()}
        conn = self.connectors.get(m.platform)
        resolver = getattr(conn, "username_by_name", None)
        if resolver is not None:
            for name in allowed:
                try:
                    u = resolver(str(name))
                except Exception:  # noqa: BLE001
                    u = None
                if u:
                    allowed_set.add(str(u))
        candidates = {m.chat_id}
        meta = m.meta or {}
        for section in ("dws", "replica"):
            section_meta = meta.get(section) or {}
            for k in ("title", "username", "conversation_id"):
                v = section_meta.get(k)
                if v:
                    candidates.add(str(v))
        return bool(candidates & allowed_set)

    def processing_snapshot(self) -> dict[str, dict]:
        """刷新/重连时恢复进行中的任务；按消息隔离，避免同联系人互相覆盖。"""
        return {mid: self._progress_copy(state) for mid, state in self._thinking.items()}

    @staticmethod
    def _progress_copy(state: dict) -> dict:
        return {**state, "steps": [dict(step) for step in state.get("steps", [])]}

    def _thinking_set(self, incoming: ChatMessage, phase: str, **extra) -> None:
        now = int(time.time() * 1000)
        state = self._thinking.setdefault(incoming.id, {
            "message_id": incoming.id, "platform": incoming.platform.value,
            "chat_id": incoming.chat_id, "since": now, "steps": [], "preview": "",
        })
        detail = str(extra.pop("detail", ""))[:240]
        delta = str(extra.pop("preview_delta", ""))
        changed = state.get("phase") != phase or state.get("detail") != detail
        state.update(phase=phase, detail=detail, updated_at=now, **extra)
        if delta:
            state["preview"] = (state["preview"] + delta)[-2400:]
        if changed:
            state["steps"] = [*state["steps"], {"phase": phase, "detail": detail, "ts": now}][-30:]
        # 文本增量合并广播，避免逐 token 淹没事件队列；阶段变化立即广播。
        last = self._progress_published.get(incoming.id, 0)
        if changed or time.monotonic() - last >= 0.2:
            self._progress_published[incoming.id] = time.monotonic()
            self.bus.publish({"type": "processing", "state": self._progress_copy(state)})

    def _thinking_done(self, incoming: ChatMessage, phase: str = "completed", detail: str = "回复草稿已生成，等待审核") -> None:
        if incoming.id not in self._thinking:
            return
        self._thinking_set(incoming, phase, detail=detail, finished_at=int(time.time() * 1000))
        state = self._thinking.pop(incoming.id)
        self._progress_published.pop(incoming.id, None)
        incoming.meta = {**incoming.meta, "processing": self._progress_copy(state)}
        self.store.update(incoming)
        self.bus.publish({"type": "message_updated", "message": incoming.model_dump(mode="json")})

    # ---------- 连发合并（debounce） ----------
    def _key(self, m: ChatMessage) -> tuple[str, str]:
        return (m.platform.value, m.chat_id)

    async def _schedule(self, msg: ChatMessage) -> None:
        key = self._key(msg)
        self._pending.setdefault(key, []).append(msg)
        old = self._timers.get(key)
        if old is not None and not old.done():
            old.cancel()  # 同会话有新消息 → 重置静默计时
        debounce = self.contact_config_for(msg.platform, msg.chat_id)["debounce_seconds"]
        if debounce <= 0:
            self._timers.pop(key, None)
            await self._flush(key)  # 关闭合并：保持同步语义（响应内完成生成）
        else:
            self._thinking_set(msg, "merging", wait=debounce, detail="等待连发消息合并")
            self._timers[key] = asyncio.create_task(self._debounce_wait(key, debounce))

    async def _debounce_wait(self, key: tuple[str, str], debounce: float) -> None:
        try:
            await asyncio.sleep(debounce)
        except asyncio.CancelledError:
            return
        self._timers.pop(key, None)
        await self._flush(key)

    async def _flush(self, key: tuple[str, str]) -> None:
        """静默期满：把该会话积压的连发消息合并为一次生成（取最后一条为触发消息）。"""
        msgs = self._pending.pop(key, [])
        if not msgs:
            return
        eligible = [m for m in msgs if self._policy_skip_reason(m) is None]
        primary = eligible[-1] if eligible else msgs[-1]
        logger.info("[agent] 静默期结束，合并 %d 条连发消息生成回复（触发: %s）", len(msgs), primary.id)
        for merged in msgs:
            if merged.id == primary.id or merged.status != "received":
                continue
            merged.status = STATUS_IGNORED
            merged.meta = {**merged.meta, "ignore_reason": f"连发合并到消息 {primary.id}"}
            self._thinking_done(merged, "skipped", f"连发合并到消息 {primary.id}")
            self.store.update(merged)
            self.bus.publish({"type": "message_updated", "message": merged.model_dump(mode="json")})
        await self._generate_and_reply(primary)

    async def _generate_and_reply(self, incoming: ChatMessage, profile: dict | None = None) -> None:
        """生成生命周期的兜底清理，包括策略跳过、异常及取消。"""
        try:
            await self._generate_reply(incoming, profile=profile)
        except asyncio.CancelledError:
            self._thinking_done(incoming, "cancelled", "处理已中断")
            raise
        except Exception:
            self._thinking_done(incoming, "failed", "处理失败，请重试或人工回复")
            raise
        finally:
            self._thinking_done(incoming)

    async def _generate_reply(self, incoming: ChatMessage, profile: dict | None = None) -> None:
        """需求(1)：监听到新消息后生成回复（全部消息场景显式处理）。"""
        # ---- 场景策略过滤 ----
        skip_reason = None if incoming.meta.get("operator_input") else self._policy_skip_reason(incoming)
        if skip_reason:
            await self._ignore(incoming, skip_reason)
            return

        # ---- 非文本消息 ----
        if incoming.msg_type != "text":
            if self.contact_config_for(incoming.platform, incoming.chat_id)["non_text_action"] == "skip":
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
        profile = profile or self.profile_for(incoming.platform, incoming.chat_id)
        cc = self.contact_config_for(incoming.platform, incoming.chat_id)
        if incoming.meta.get("debug_profile_id") and profile:
            cc["timeout"] = float(profile.get("timeout") or 300)
        self._thinking_set(
            incoming, "preparing", detail="正在准备上下文",
            agent=profile["name"] if profile else ("默认 Agent" if self.engine.name == "acp" else self.engine.name),
            timeout=cc["timeout"],
        )
        history = self._session_history(incoming)
        try:
            engine = self.engine_for_profile(profile) if profile else self.engine
            self._apply_session_token(engine, incoming.platform, incoming.chat_id)
            async with asyncio.timeout(cc["timeout"]):
                if isinstance(engine, ACPAgentGenerator):
                    result = await engine.generate(
                        incoming, history, kb=self.kb_for(incoming.platform),
                        on_progress=lambda phase, **extra: self._thinking_set(incoming, phase, **extra),
                    )
                else:
                    self._thinking_set(incoming, "thinking", detail="正在等待 AI 生成回复")
                    result = await engine.generate(incoming, history, kb=self.kb_for(incoming.platform))
        except Exception as exc:  # noqa: BLE001
            # 降级：引擎不可用时生成错误占位草稿，进入人工流程（消息绝不静默丢失）
            error = f"超过 {cc['timeout']:g} 秒仍未完成，已停止等待" if isinstance(exc, TimeoutError) else str(exc)[:200]
            self._thinking_done(incoming, "failed", error or "AI 调用失败")
            self._record_llm_outcome(False, error)
            logger.exception("生成回复失败: %s", error)
            reply = ChatMessage(
                platform=incoming.platform,
                chat_id=incoming.chat_id,
                sender="assistant",
                content="⚠️ AI 引擎暂时不可用，请人工回复这条消息",
                direction=DIRECTION_OUT,
                status="debug" if incoming.meta.get("debug_profile_id") else STATUS_PENDING,
                reply_to=incoming.id,
                meta={"engine": "error", "error": error, **({"debug_profile_id": profile["id"]} if incoming.meta.get("debug_profile_id") else {})},
            )
            self._apply_reply_routing(reply, incoming)
            self.store.add(reply)
            self.bus.publish({"type": "reply", "message": reply.model_dump(mode="json")})
            return
        self._record_llm_outcome(True)

        # 普通草稿默认待审核；开启 auto_send 后才进入投递流程。
        status = "debug" if incoming.meta.get("debug_profile_id") else STATUS_PENDING
        if incoming.meta.get("debug_profile_id"):
            result.meta["debug_profile_id"] = profile["id"]

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
            "（仅调试）" if status == "debug" else ("（自动投递中）" if self.settings.auto_send and not incoming.meta.get("operator_input") else "（等待人工确认）"),
        )
        self._thinking_done(incoming, detail="调试回复已生成" if status == "debug" else "回复草稿已生成，等待审核")
        if self.settings.auto_send and status == STATUS_PENDING and not incoming.meta.get("operator_input"):
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
                    self._thinking_set(incoming, "thinking", agent="图片识别", detail="正在识别图片内容")
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
        if incoming.meta.get("session_token"):
            reply.meta = {**reply.meta, "session_token": incoming.meta["session_token"]}
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
    def validate_config_updates(self, updates: dict) -> None:
        """验证整批热更新字段，保证调用方可以先校验再持久化。"""
        s = self.settings
        if updates.get("llm_base_url") is not None:
            try:
                url = urlsplit(updates["llm_base_url"].strip())
                valid = url.scheme in ("http", "https") and bool(url.hostname) and not (url.username or url.password or url.query or url.fragment)
                url.port  # 校验端口
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("API URL 必须是有效的 HTTP(S) Base URL，不包含凭据、查询参数或片段")
        if updates.get("llm_model") is not None and not updates["llm_model"].strip():
            raise ValueError("模型名称不能为空")
        if updates.get("engine") is not None and updates["engine"] not in ("mock", "llm", "acp"):
            raise ValueError("engine 仅支持 mock / llm / acp")
        if updates.get("acp_mode") is not None and updates["acp_mode"] not in ("acp", "pi"):
            raise ValueError("acp_mode 仅支持 acp / pi")
        for key in ("acp_command", "acp_args", "acp_cwd", "acp_env_name", "acp_env_key"):
            if updates.get(key) is not None:
                updates[key] = str(updates[key]).strip()
        if updates.get("acp_cwd") and not Path(updates["acp_cwd"]).expanduser().is_dir():
            raise ValueError(f"Agent 工作空间目录不存在: {updates['acp_cwd']}")
        if (updates.get("engine", s.engine) == "acp"
                and not updates.get("acp_command", s.acp.command).strip()):
            raise ValueError("engine=acp 需要先配置 Agent 启动命令（acp_command）")
        if updates.get("acp_timeout") is not None:
            if not (5.0 <= float(updates["acp_timeout"]) <= 3600):
                raise ValueError("acp_timeout 范围 5-3600 秒")
        if updates.get("num_candidates") is not None and not 1 <= int(updates["num_candidates"]) <= 5:
            raise ValueError("num_candidates 范围 1-5")
        if updates.get("temperature") is not None:
            float(updates["temperature"])
        if updates.get("llm_max_retries") is not None:
            int(updates["llm_max_retries"])
        if updates.get("llm_rate_limit_per_chat") is not None:
            int(updates["llm_rate_limit_per_chat"])
        if updates.get("debounce_seconds") is not None:
            float(updates["debounce_seconds"])
        if updates.get("llm_style") is not None:
            from ..engine.llm import STYLE_PRESETS
            if updates["llm_style"] not in STYLE_PRESETS:
                raise ValueError(f"llm_style 仅支持 {' / '.join(STYLE_PRESETS)}")
        if updates.get("group_trigger") is not None and updates["group_trigger"] not in ("at-mention", "all", "listen-only"):
            raise ValueError("group_trigger 仅支持 at-mention / all / listen-only")
        if updates.get("non_text_action") is not None and updates["non_text_action"] not in ("placeholder", "skip"):
            raise ValueError("non_text_action 仅支持 placeholder / skip")
        if updates.get("alert_channel") is not None and updates["alert_channel"] not in ("off", "wechat-filehelper", "dingtalk-self"):
            raise ValueError("alert_channel 仅支持 off / wechat-filehelper / dingtalk-self")
        if updates.get("knowledge_dir"):
            if not Path(str(updates["knowledge_dir"]).strip()).expanduser().is_dir():
                raise ValueError(f"知识库目录不存在或不是文件夹: {updates['knowledge_dir']}")
        for key in ("wechat_knowledge_dir", "dingtalk_knowledge_dir"):
            if updates.get(key):
                if not Path(str(updates[key]).strip()).expanduser().is_dir():
                    raise ValueError(f"知识库目录不存在或不是文件夹: {updates[key]}")
        if updates.get("embedding_enabled") and not s.embedding.model:
            raise ValueError("embedding.model 未配置，无法启用语义检索")

    async def apply_config(self, updates: dict) -> dict:
        """热更新配置（子集），返回变更清单。不支持的项抛 ValueError。"""
        self.validate_config_updates(updates)
        s = self.settings
        changed: list[str] = []
        rebuild = False

        engine = updates.get("engine")
        if engine is not None:
            if engine not in ("mock", "llm", "acp"):
                raise ValueError("engine 仅支持 mock / llm / acp")
            if engine != s.engine:
                s.engine = engine
                rebuild = True
                changed.append("engine")

        llm_config_changed = False
        for key, attr in (("llm_base_url", "base_url"), ("llm_model", "model"), ("llm_api_key", "api_key")):
            value = updates.get(key)
            if value is not None and str(value).strip():
                normalized = str(value).strip()
                if getattr(s.llm, attr) != normalized:
                    setattr(s.llm, attr, normalized)
                    llm_config_changed = True
                    changed.append(key)
        if llm_config_changed:
            rebuild = True

        acp_changed = False
        for key, attr in (("acp_mode", "mode"), ("acp_command", "command"), ("acp_args", "args"),
                          ("acp_cwd", "cwd"), ("acp_env_name", "env_name"), ("acp_env_key", "env_key")):
            value = updates.get(key)
            if value is not None:
                normalized = str(value).strip()
                if getattr(s.acp, attr) != normalized:
                    setattr(s.acp, attr, normalized)
                    acp_changed = True
                    changed.append(key)
        if updates.get("acp_timeout") is not None:
            tv = max(5.0, float(updates["acp_timeout"]))
            if s.acp.timeout != tv:
                s.acp.timeout = tv
                acp_changed = True
                changed.append("acp_timeout")
        if acp_changed:
            rebuild = True
        # engine=acp 必须有启动命令（首次切换或修改 acp 配置时校验）
        if s.engine == "acp" and (updates.get("engine") == "acp" or acp_changed) and not s.acp.command.strip():
            raise ValueError("engine=acp 需要先配置 Agent 启动命令（acp_command）")

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
        for key, platform in (("wechat_knowledge_dir", Platform.WECHAT), ("dingtalk_knowledge_dir", Platform.DINGTALK)):
            if updates.get(key) is not None:
                path = str(updates[key]).strip()
                cs = getattr(s, platform.value)
                cs.knowledge_dir = path
                self._rebuild_platform_kb(platform)  # 空路径回退全局库；目录不存在抛 ValueError → 400
                changed.append(key)
        if updates.get("embedding_enabled") is not None:
            s.embedding.enabled = bool(updates["embedding_enabled"])
            try:
                for kb_inst in [self.kb, *self.kbs.values()]:
                    kb_inst.set_embedding(
                        s.embedding,
                        llm_base_url=s.llm.base_url,
                        llm_api_key=s.llm.api_key,
                        cache_path=self._embedding_cache_path(
                            next((p for p, k in self.kbs.items() if k is kb_inst), None)
                        ),
                    )
                changed.append("embedding_enabled")
            except ValueError as exc:
                s.embedding.enabled = not s.embedding.enabled  # 回滚，保持配置一致
                raise ValueError(f"embedding 配置无效: {exc}") from exc

        if updates.get("group_trigger") is not None:
            s.group_trigger = updates["group_trigger"]
            changed.append("group_trigger")
        if updates.get("non_text_action") is not None:
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
        for key, platform in (("wechat_allowed_contacts", Platform.WECHAT), ("dingtalk_allowed_contacts", Platform.DINGTALK)):
            if updates.get(key) is not None:
                cs = getattr(s, platform.value)
                cs.allowed_contacts = [str(x).strip() for x in updates[key] if str(x).strip()]
                changed.append(key)
        if any(key in changed for key in ("allowed_contacts", "wechat_allowed_contacts", "dingtalk_allowed_contacts")):
            self.prune_removed_contacts()
        if updates.get("auto_send") is not None:
            s.auto_send = bool(updates["auto_send"])
            changed.append("auto_send")

        if rebuild:
            old_engine = self.engine
            self.engine = build_engine(s, kb=self.kb)
            try:
                await old_engine.aclose()
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
        """返回需要忽略的原因；None 表示正常处理（群聊策略按联系人配置）。"""
        if m.msg_type == "system":
            return "系统消息不生成回复"
        trigger = self.contact_config_for(m.platform, m.chat_id)["group_trigger"]
        if m.is_group and trigger == "listen-only":
            return "群聊策略为只监听不回复"
        if m.is_group and trigger == "at-mention" and not m.at_me:
            return "群聊消息未@我（策略：仅回复@我）"
        return None

    async def _ignore(self, m: ChatMessage, reason: str) -> None:
        """按策略忽略：标记状态并广播，不生成草稿。"""
        m.status = STATUS_IGNORED
        m.meta = {**m.meta, "ignore_reason": reason}
        self.store.update(m)
        self.bus.publish({"type": "message_updated", "message": m.model_dump(mode="json")})
        self._thinking_done(m, "skipped", reason)
        logger.info("[agent] 消息已忽略(%s): %s", reason, m.id)

    async def _dispatch(self, reply: ChatMessage) -> str:
        """统一投递入口，返回 sent/failed/unknown，且先广播 sending 状态。"""
        if reply.platform == Platform.WEB or reply.meta.get("debug_profile_id"):
            raise ValueError("调试回复仅供本地查看，不能发送到平台")
        cleaned = clean_agent_reply(reply.content)
        if not cleaned:
            raise ValueError("回复没有可发送的正文，请编辑后再发送")
        reply.content = cleaned
        conn = self.connectors.get(reply.platform)
        reply.status = STATUS_SENDING
        self.store.update(reply)
        self.bus.publish({"type": "reply_updated", "message": reply.model_dump(mode="json")})
        if conn is None:
            logger.warning("平台 %s 无可用连接器，回复未发送", reply.platform)
            result = STATUS_FAILED
        else:
            try:
                result = STATUS_SENT if await conn.send_reply(reply) else STATUS_FAILED
            except Exception as exc:  # noqa: BLE001
                logger.warning("回复投递结果不确定: %s", exc)
                reply.meta = {**reply.meta, "dispatch_error": str(exc)[:200]}
                result = STATUS_UNKNOWN
        reply.status = result
        self.store.update(reply)
        self.bus.publish({"type": "reply_updated", "message": reply.model_dump(mode="json")})
        return result

    # ---------- 人工审核（唯一的发送路径） ----------
    async def approve_reply(
        self, reply_id: str, content: str | None = None, confirm_unknown: bool = False
    ) -> ChatMessage:
        """人工确认发送；结果不确定的投递必须由人工明确确认后才能重试。"""
        reply = self.store.get(reply_id)
        if reply is None or reply.direction != DIRECTION_OUT:
            raise KeyError(reply_id)
        if reply.platform == Platform.WEB or reply.meta.get("debug_profile_id"):
            raise ValueError("调试回复仅供本地查看，不能发送到平台")
        allowed = (STATUS_PENDING, STATUS_BLOCKED, STATUS_FAILED)
        if reply.status == STATUS_UNKNOWN and not confirm_unknown:
            raise ValueError("上次投递结果不确定，请核对平台后再次确认重试")
        if reply.status not in allowed and reply.status != STATUS_UNKNOWN:
            raise ValueError(f"回复当前状态为 {reply.status}，不能发送")
        if content is not None and content.strip() and content != reply.content:
            original = reply.content
            reply.content = content.strip()
            reply.meta = {**reply.meta, "edited_from": original}
        result = await self._dispatch(reply)
        self.store.audit(
            "admin", "approve", reply.id,
            json.dumps({"content": reply.content, "edited_from": reply.meta.get("edited_from"),
                        "result": result}, ensure_ascii=False),
        )
        return reply

    async def select_candidate(self, reply_id: str, index: int) -> ChatMessage:
        """多候选切换：把第 index 条候选设为草稿正文（原内容保留在 edited_from）。"""
        reply = self.store.get(reply_id)
        if reply is None or reply.direction != DIRECTION_OUT:
            raise KeyError(reply_id)
        if reply.status not in (STATUS_PENDING, STATUS_BLOCKED, STATUS_FAILED, STATUS_UNKNOWN):
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
        if reply.status not in (STATUS_PENDING, STATUS_BLOCKED, STATUS_FAILED, STATUS_UNKNOWN):
            raise ValueError(f"回复当前状态为 {reply.status}，不能驳回")
        reply.status = STATUS_REJECTED
        self.store.update(reply)
        self.store.audit("admin", "reject", reply.id, reply.content[:100])
        self.bus.publish({"type": "reply_updated", "message": reply.model_dump(mode="json")})
        return reply

    # ---------- 直接对话（前端“对话测试”，走同一个生成器接口） ----------
    async def direct_chat(
        self, content: str, chat_id: str = "web-demo", platform: str | None = None
    ) -> tuple[ChatMessage, ChatMessage]:
        # 可选指定平台：用该平台的知识库测试（如钉钉独立知识库）；web 用全局库
        plat = Platform(platform) if platform in ("wechat", "dingtalk") else Platform.WEB
        kb = self.kb_for(plat) if plat != Platform.WEB else None
        user_msg = ChatMessage(platform=Platform.WEB, chat_id=chat_id, sender="user", content=content)
        self.store.add(user_msg)
        self.bus.publish({"type": "message", "message": user_msg.model_dump(mode="json")})
        history = [
            h for h in self.store.history(Platform.WEB, chat_id, limit=self.settings.llm.max_history)
            if h.id != user_msg.id
        ]
        result = await self.engine.generate(user_msg, history, kb=kb)
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
    def _embedding_cache_path(self, platform: Platform | None = None) -> str:
        """向量缓存文件路径：与数据库同目录（db 为 :memory: 时用当前目录）；平台专属库独立缓存避免混写。"""
        db = Path(self.settings.db_path)
        base_dir = db.parent if str(db.parent) != "." else Path(".")
        name = f"rag_embeddings.{platform.value}.json" if platform else "rag_embeddings.json"
        return str(base_dir / name)

    async def shutdown(self) -> None:
        tasks = list(self.profile_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.profile_tasks.clear()
        # 尽力冲刷未处理的连发积压，避免丢草稿（消息已落盘，草稿生成后也会落盘）
        for key in list(self._pending.keys()):
            try:
                await self._flush(key)
            except Exception:  # noqa: BLE001
                logger.exception("[agent] 关闭时冲刷积压消息失败: %s", key)
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        if self._recover_task is not None:
            self._recover_task.cancel()
            self._recover_task = None
        if self._watch_task is not None:
            self._watch_task.cancel()
            self._watch_task = None
        await self.stop_all()
        for engine in [self.engine, *self.profile_engines.values()]:
            try:
                await engine.aclose()
            except Exception:  # noqa: BLE001
                logger.exception("[agent] 关闭 Agent 引擎失败")
        self.profile_engines.clear()
        if self._vision is not None:
            try:
                await self._vision.aclose()
            except Exception:  # noqa: BLE001
                pass
        for kb_inst in [self.kb, *self.kbs.values()]:
            try:
                await kb_inst.aclose()
            except Exception:  # noqa: BLE001
                pass
