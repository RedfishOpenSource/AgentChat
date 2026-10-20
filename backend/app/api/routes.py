"""REST API 路由。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request

from ..agents_registry import list_agents
from ..config import PANEL_SETTINGS_FIELDS, mask_settings, panel_settings, save_config_partial
from ..connectors.base import ConnectorStartError
from ..connectors.mock import MockConnector, is_mock_connector
from ..models import (
    AgentAssignmentRequest,
    AgentDebugRequest,
    AgentMcpOpenRequest,
    AgentProfileRequest,
    ApproveRequest,
    CandidateSelectRequest,
    ConfigUpdateRequest,
    ContactsRequest,
    ContactConfigRequest,
    ContactChatRequest,
    DirectChatRequest,
    Platform,
    RegenerateRequest,
    SessionResetRequest,
    SimulateRequest,
)
from ..core.agent import AgentService
from ..engine.output import clean_agent_reply
from ..persistence import SqliteStore

router = APIRouter(prefix="/api")

logger = logging.getLogger(__name__)


def _agent(request: Request) -> AgentService:
    return request.app.state.agent


def _store(request: Request):
    return request.app.state.store


def _message_payload(message) -> dict:
    """兼容旧版已落库的 ACP 横幅，展示正文与发送正文保持一致。"""
    payload = message.model_dump(mode="json")
    if message.direction == "out" and message.meta.get("engine") == "acp":
        payload["content"] = clean_agent_reply(payload["content"])
    return payload


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/agents")
async def list_available_agents():
    """可下拉选择的现成 Agent（均经 ACP 接入），含本机可用性检测结果。"""
    return {"agents": list_agents()}


# ---- Agent 配置（Agent Studio）与联系人分配 ----


def _engine_available(command: str) -> bool:
    import shutil
    return bool(command.strip()) and shutil.which(command.strip()) is not None


async def _llm_role_prompt(settings, description: str) -> str:
    from ..role_prompts import rewrite_role
    return await rewrite_role(settings, description)


@router.get("/agent-profiles")
async def list_agent_profiles(request: Request):
    agent = _agent(request)
    profiles = []
    for p in agent.agent_profiles.values():
        profiles.append({**p, "available": _engine_available(p.get("command", ""))})
    return {"profiles": profiles, "assignments": agent.agent_assignments_list()}


@router.post("/agent-profiles")
async def upsert_agent_profile(request: Request, body: AgentProfileRequest):
    """创建/更新 Agent 配置（n8n 式节点化配置，保存后立即生效）。"""
    import uuid
    from pathlib import Path as _Path

    agent = _agent(request)
    data = body.model_dump()
    data["id"] = (body.id or f"agent-{uuid.uuid4().hex[:8]}").strip()
    data["name"] = body.name.strip() or "未命名 Agent"
    data["command"] = body.command.strip()
    if not data["command"]:
        preset = {a["id"]: a for a in list_agents()}.get(body.engine)
        data["command"] = preset["command"] if preset else body.engine
        data["args"] = data.get("args") or ""
    if not _Path(data["workspace"]).expanduser().is_dir():
        raise HTTPException(status_code=400, detail=f"工作空间目录不存在: {data['workspace']}")
    if body.knowledge_dir and not _Path(body.knowledge_dir).expanduser().is_dir():
        raise HTTPException(status_code=400, detail=f"知识库目录不存在: {body.knowledge_dir}")
    if not (5 <= float(body.timeout) <= 3600):
        raise HTTPException(status_code=400, detail="timeout 范围 5-3600 秒")

    # 每次新建/保存都重新改写，包括内容未变、仅修改设置、调用方传了 instructions。
    existing = agent.agent_profiles.get(data["id"]) if body.id else None
    source = data["description"].strip() or data["instructions"].strip()
    if not source:
        raise HTTPException(status_code=400, detail="请填写 Agent 描述，用于 AI 角色改写")
    revision = uuid.uuid4().hex
    data.update(description_raw=source, rewrite_revision=revision,
                rewrite_status="pending", rewrite_error="", pending=True)
    data["instructions"] = (existing or {}).get("instructions", "")
    ws = _Path(data["workspace"]).expanduser()
    role_file = ws / ".agents" / "memory" / "role.md"
    data["role_file"] = str(role_file)

    # Skills 落地：URL → git clone；本地目录 → 复制到 <ws>/.agents/skills/
    import shutil as _shutil
    import subprocess as _subprocess

    skills_root = ws / ".agents" / "skills"
    try:
        skills_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"创建 skills 目录失败: {exc}") from None
    provisioned: list[str] = []
    for src in data.get("skills", []):
        src = str(src).strip()
        if not src:
            continue
        if src.startswith(("http://", "https://", "git@")):
            name = src.rstrip("/").split("/")[-1].removesuffix(".git")
            dest = skills_root / name
            if not dest.exists():
                try:
                    r = _subprocess.run(
                        ["git", "clone", "--depth", "1", src, str(dest)],
                        capture_output=True, text=True, timeout=180, check=False,
                    )
                except (OSError, _subprocess.TimeoutExpired) as exc:
                    raise HTTPException(status_code=400, detail=f"拉取 skill 失败: {src}: {exc}") from None
                if r.returncode:
                    raise HTTPException(status_code=400, detail=f"拉取 skill 失败: {src}: {(r.stderr or r.stdout)[-250:]}")
        else:
            sp = _Path(src).expanduser()
            if not sp.is_dir():
                raise HTTPException(status_code=400, detail=f"skill 目录不存在: {src}")
            dest = skills_root / sp.name
            if sp.resolve() != dest.resolve():
                if dest.resolve().is_relative_to(sp.resolve()) or sp.resolve().is_relative_to(dest.resolve()):
                    raise HTTPException(status_code=400, detail="Skill 源目录与安装目录不能互相包含")
                if dest.exists():
                    _shutil.rmtree(dest)
                _shutil.copytree(sp, dest)
        provisioned.append(dest.name)
    data["skills_dir"] = str(skills_root)

    # MCP 配置文件：<ws>/.agents/mcps/.mcp.json（不存在则初始化）
    mcp_file = ws / ".agents" / "mcps" / ".mcp.json"
    try:
        mcp_file.parent.mkdir(parents=True, exist_ok=True)
        if not mcp_file.exists():
            mcp_file.write_text(json.dumps({"mcpServers": {}}, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"初始化 MCP 配置失败: {exc}") from None
    data["mcp_file"] = str(mcp_file)

    old_task = agent.profile_tasks.pop(data["id"], None)
    if old_task:
        old_task.cancel()
    agent.save_agent_profile(dict(data))

    # ---- 后台收尾：AI 提示词改写 + GitHub 案例同步 + role.md 覆盖（不阻塞创建） ----
    import time as _time
    from ..prompts_sync import mark_synced, prompts_recent, sync_workspace_prompts

    def current() -> bool:
        return agent.agent_profiles.get(data["id"], {}).get("rewrite_revision") == revision

    async def _finalize() -> str:
        from ..role_prompts import RoleRewriteError, write_role_file
        try:
            optimized = await _llm_role_prompt(agent.settings, source)
            if not optimized.strip() or optimized.strip() == source:
                raise RoleRewriteError("AI 未改写角色描述，请重新保存重试")
            if not current():
                return "该保存任务已被新版本替代"
            write_role_file(role_file, data["name"], optimized)
            data.update(description=optimized, instructions=optimized, pending=False,
                        rewrite_status="succeeded", rewrite_error="",
                        rewritten_at=int(_time.time() * 1000))
            agent.save_agent_profile(dict(data))
        except asyncio.CancelledError:
            if current():
                data.update(pending=False, rewrite_status="failed", rewrite_error="角色改写被中断，请重新保存")
                agent.save_agent_profile(dict(data))
            raise
        except Exception as exc:
            if current():
                reason = str(exc) if isinstance(exc, RoleRewriteError) else "角色文件写入或改写失败，请重新保存并检查服务日志"
                data.update(pending=False, rewrite_status="failed", rewrite_error=reason)
                agent.save_agent_profile(dict(data))
            logger.warning("[agent-role] 改写失败 id=%s type=%s", data["id"], type(exc).__name__)
            return "角色改写失败，请查看 Agent 的错误信息后重试"
        if not prompts_recent(data["workspace"]):
            try:
                await sync_workspace_prompts(data["workspace"])
                mark_synced(data["workspace"])
            except Exception as exc:
                logger.warning("[prompts-sync] 后台同步失败 type=%s", type(exc).__name__)
        return "AI 角色已重新改写并写入 role.md"

    if body.sync_mode == "wait":
        sync_note = await _finalize()
    else:
        task = asyncio.create_task(_finalize())
        agent.profile_tasks[data["id"]] = task
        def forget(completed):
            if agent.profile_tasks.get(data["id"]) is completed:
                agent.profile_tasks.pop(data["id"], None)
        task.add_done_callback(forget)
        sync_note = "已触发 AI 角色重新改写，请等待处理结果"

    return {
        "profile": {**data, "available": _engine_available(data["command"])},
        "prompts_note": sync_note,
    }


@router.delete("/agent-profiles/{agent_id}")
async def remove_agent_profile(agent_id: str, request: Request):
    """删除 Agent；若仍被任何监听联系人绑定，则拒绝删除。"""
    agent = _agent(request)
    used = [a for a in agent.agent_assignments_list() if a["agent_id"] == agent_id]
    if used:
        names = "、".join(f"{a['platform']}:{a['chat_id']}" for a in used[:5])
        more = "等" if len(used) > 5 else ""
        raise HTTPException(
            status_code=400,
            detail=f"该 Agent 正在被监听联系人使用（{names}{more}），请先在监听联系人中改绑后再删除",
        )
    agent.delete_agent_profile(agent_id)
    return {"deleted": agent_id}


@router.post("/agent-mcp/open")
async def open_agent_mcp(request: Request, body: AgentMcpOpenRequest):
    """确保 .agents/mcps/.mcp.json 存在，并用系统编辑器（如 VS Code）打开。"""
    import shutil as _shutil
    import subprocess as _subprocess

    ws = Path(body.workspace).expanduser()
    if not ws.is_dir():
        raise HTTPException(status_code=400, detail=f"工作空间不存在: {body.workspace}")
    p = ws / ".agents" / "mcps" / ".mcp.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(json.dumps({"mcpServers": {}}, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"初始化 MCP 配置失败: {exc}") from None
    editor = _shutil.which("code")
    try:
        if editor:
            _subprocess.Popen([editor, str(p)])
        elif sys.platform == "win32":
            os.startfile(str(p))  # noqa: S606 —— Windows 默认编辑器
        else:
            _subprocess.Popen(["open", str(p)])
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"无法打开编辑器: {exc}") from None
    return {"path": str(p)}


@router.post("/agent-sessions/reset")
async def reset_agent_session(request: Request, body: SessionResetRequest):
    """清空指定联系人的 Agent 会话：下条消息将开启全新会话（本地聊天记录保留）。"""
    if body.platform not in (Platform.WECHAT.value, Platform.DINGTALK.value):
        raise HTTPException(status_code=400, detail="platform 仅支持 wechat / dingtalk")
    if not body.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id 不能为空")
    agent = _agent(request)
    platform, chat_id = Platform(body.platform), body.chat_id.strip()
    if agent.contact_processing(platform, chat_id):
        raise HTTPException(status_code=409, detail="该对话正在处理中，请等待完成后再清空会话")
    cleared = agent.reset_contact_session(platform, chat_id)
    boundary = agent.session_resets[(platform.value, chat_id)]
    return {"platform": platform.value, "chat_id": chat_id, "reset": cleared,
            "session_token": boundary["token"], "reset_at": boundary["reset_at"]}


@router.post("/agent-profiles/{agent_id}/chat")
async def debug_agent_profile(agent_id: str, request: Request, body: AgentDebugRequest):
    agent = _agent(request)
    profile = agent.agent_profiles.get(agent_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    if profile.get("pending"):
        raise HTTPException(status_code=409, detail="Agent 配置正在后台处理中，请稍后调试")
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息内容不能为空")
    incoming, reply = await agent.debug_chat(profile, body.session_id, content)
    return {"message": incoming.model_dump(mode="json"),
            "reply": reply.model_dump(mode="json") if reply else None}


@router.post("/agent-sessions/chat")
async def contact_chat(request: Request, body: ContactChatRequest):
    """向当前联系人会话补充消息；结果始终为待审核草稿。"""
    if body.platform not in (Platform.WECHAT.value, Platform.DINGTALK.value):
        raise HTTPException(status_code=400, detail="platform 仅支持 wechat / dingtalk")
    chat_id, content = body.chat_id.strip(), body.content.strip()
    if not chat_id or not content:
        raise HTTPException(status_code=400, detail="请选择联系人并填写消息内容")
    agent = _agent(request)
    platform = Platform(body.platform)
    if not agent.profile_for(platform, chat_id):
        raise HTTPException(status_code=400, detail="请先在监听联系人中为该对话绑定 Agent")
    incoming, reply = await agent.contact_chat(platform, chat_id, content)
    return {"message": incoming.model_dump(mode="json"),
            "reply": reply.model_dump(mode="json") if reply else None}


@router.post("/messages/{message_id}/regenerate")
async def regenerate_reply(message_id: str, request: Request, body: RegenerateRequest):
    """对某条收到的消息补充说明后重新生成回复（生成新的待审核草稿）。"""
    origin = _store(request).get(message_id)
    if origin is None or origin.direction != "in":
        raise HTTPException(status_code=404, detail=f"未找到该消息: {message_id}")
    extra = body.extra.strip()
    if not extra:
        raise HTTPException(status_code=400, detail="补充内容不能为空")
    reply = await _agent(request).regenerate_with_supplement(origin, extra)
    return {"origin_id": message_id, "reply": reply.model_dump(mode="json")}


@router.put("/agent-assignments")
async def assign_contact_agent(request: Request, body: AgentAssignmentRequest):
    """给联系人（平台+会话）绑定 Agent；agent_id 为空串 = 恢复默认引擎。"""
    agent = _agent(request)
    if body.agent_id and body.agent_id not in agent.agent_profiles:
        raise HTTPException(status_code=404, detail=f"Agent 不存在: {body.agent_id}")
    agent.assign_agent(body.platform, body.chat_id, body.agent_id or None)
    return {"platform": body.platform, "chat_id": body.chat_id, "agent_id": body.agent_id or None}


@router.get("/contact-configs")
async def get_contact_configs(request: Request, platform: str | None = None):
    """联系人监听配置（未设置的联系人回退全局默认，不在返回中体现）。"""
    agent = _agent(request)
    items = [
        {"platform": p, "chat_id": chat_id, **cfg}
        for (p, chat_id), cfg in agent.contact_configs.items()
        if not platform or p == platform
    ]
    return {"configs": items}


@router.put("/contact-configs")
async def put_contact_config(request: Request, body: ContactConfigRequest):
    """保存单个联系人的监听配置（超时/群聊触发/非文本/连发合并）。"""
    agent = _agent(request)
    if body.platform not in (Platform.WECHAT.value, Platform.DINGTALK.value):
        raise HTTPException(status_code=400, detail="platform 仅支持 wechat / dingtalk")
    if not (0 <= body.debounce_seconds <= 300):
        raise HTTPException(status_code=400, detail="debounce_seconds 范围 0-300 秒")
    if not (5 <= body.timeout <= 3600):
        raise HTTPException(status_code=400, detail="timeout 范围 5-3600 秒")
    if body.group_trigger not in ("at-mention", "all", "listen-only"):
        raise HTTPException(status_code=400, detail="group_trigger 不合法")
    if body.non_text_action not in ("placeholder", "skip"):
        raise HTTPException(status_code=400, detail="non_text_action 不合法")
    config = {
        "timeout": body.timeout,
        "group_trigger": body.group_trigger,
        "non_text_action": body.non_text_action,
        "debounce_seconds": body.debounce_seconds,
    }
    agent.save_contact_config(body.platform, body.chat_id, config)
    return {"platform": body.platform, "chat_id": body.chat_id, **config}


@router.delete("/contact-configs")
async def remove_contact_config(request: Request, platform: str, chat_id: str):
    _agent(request).delete_contact_config(platform, chat_id)
    return {"deleted": {"platform": platform, "chat_id": chat_id}}


@router.get("/status")
async def status(request: Request):
    agent = _agent(request)
    return {
        "engine": agent.engine.name,
        # engine=acp 时显示当前选中的 Agent 启动命令，供前端芯片展示
        "agent_command": (getattr(agent.engine, "cfg", None).command or "") if agent.engine.name == "acp" else "",
        # 发送模式：auto_send=False（默认）仅生成草稿；True 自动发送（前端按钮切换）
        "auto_send": agent.settings.auto_send,
        "send_mode": "auto" if agent.settings.auto_send else "manual_confirm",
        # 各平台生效白名单（独立名单优先，未配置回退全局）；空数组 = 允许全部
        "allowed_contacts": {
            p.value: agent.effective_contacts(p) for p in (Platform.WECHAT, Platform.DINGTALK)
        },
        "allowed_contacts_global": agent.settings.allowed_contacts,
        "alerts": agent.alerts.active_list(),
        "connectors": {p.value: c.info() for p, c in agent.connectors.items()},
        "stats": {"messages": _store(request).count_all()},
    }


@router.get("/audit")
async def list_audit(request: Request, limit: int = 50):
    """审计日志（最近的发送/驳回操作）。"""
    db: SqliteStore | None = getattr(request.app.state, "db", None)
    if db is None:
        return {"audit": []}
    return {"audit": db.list_audit(limit=max(1, min(limit, 500)))}


@router.get("/contacts")
async def get_contacts(request: Request, platform: str | None = None):
    """联系人白名单：不传 platform 返回全局默认（空 = 允许全部）；传平台返回该平台生效名单。"""
    agent = _agent(request)
    if platform:
        p = _parse_platform(platform)
        return {"platform": p.value, "allowed_contacts": agent.effective_contacts(p)}
    return {"allowed_contacts": agent.settings.allowed_contacts}


def _parse_platform(platform: str) -> Platform:
    try:
        return Platform(platform)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"未知平台 {platform}") from None


@router.get("/contacts/suggestions")
async def contact_suggestions(request: Request, platform: str):
    """联系人选择建议：会话列表（微信 replica）+ 最近消息里出现过的联系人，供前端弹窗选择。"""
    agent = _agent(request)
    p = _parse_platform(platform)
    items: dict[str, dict] = {}
    conn = agent.connectors.get(p)
    if hasattr(conn, "list_contacts"):
        try:
            for name, username in await conn.list_contacts():
                if name or username:
                    items[username or name] = {"name": name, "username": username, "source": "会话列表"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[contacts] 读取会话列表失败: %s", exc)
    for m in _store(request).list_messages(direction="in", limit=1000):
        if m.platform != p or not m.chat_id:
            continue
        meta = m.meta or {}
        username = ""
        for section in ("replica", "dws"):
            section_meta = meta.get(section) or {}
            username = str(section_meta.get("username") or section_meta.get("conversation_id") or "")
            if username:
                break
        value = username or m.chat_id
        if value not in items:
            items[value] = {"name": m.chat_id, "username": username, "source": "消息记录"}
    return {"platform": p.value, "suggestions": list(items.values())}


@router.put("/contacts")
async def set_contacts(request: Request, body: ContactsRequest, platform: str | None = None):
    """更新联系人白名单：立即生效，并写回配置文件避免重启失效。

    - platform=wechat/dingtalk：只更新该平台独立名单（覆盖全局）
    - 不传 platform：更新全局默认名单（各平台未单独配置时生效）
    - 未配置配置文件路径（如单测）时仅改内存，响应 persisted=False
    """
    agent = _agent(request)
    cleaned = [x.strip() for x in body.allowed_contacts if str(x).strip()]
    if platform:
        p = _parse_platform(platform)
        cs = getattr(agent.settings, p.value)
        cs.allowed_contacts = cleaned
        yaml_updates: dict = {p.value: {"allowed_contacts": cleaned}}
        result: dict = {"platform": p.value, "allowed_contacts": agent.effective_contacts(p)}
    else:
        agent.settings.allowed_contacts = cleaned
        yaml_updates = {"allowed_contacts": cleaned}
        result = {"allowed_contacts": agent.settings.allowed_contacts}
    persisted = False
    if agent.settings.config_file:
        try:
            save_config_partial(yaml_updates, agent.settings.config_file)
            persisted = True
        except OSError as exc:
            result["persist_error"] = str(exc)[:200]
    result["persisted"] = persisted
    return result


def _config_yaml_updates(updates: dict) -> dict:
    """把 API 字段映射为 config.yaml 的真实嵌套结构。"""
    out: dict = {}
    for key in ("engine", "auto_send", "group_trigger", "non_text_action", "debounce_seconds",
                "llm_rate_limit_per_chat", "alert_channel", "alert_dingtalk_chat_id",
                "allowed_contacts", "knowledge_dir"):
        if key in updates:
            out[key] = updates[key]
    # 连接器级独立配置：映射为 config.yaml 的 wechat:/dingtalk: 子节点
    for section in ("wechat", "dingtalk"):
        sub = {k.split("_", 1)[1]: updates[k]
               for k in (f"{section}_allowed_contacts", f"{section}_knowledge_dir") if k in updates}
        if sub:
            out[section] = {**out.get(section, {}), **sub}
    llm = {key: updates[key] for key in ("num_candidates", "temperature") if key in updates}
    for source, target in (("llm_base_url", "base_url"), ("llm_model", "model"), ("llm_api_key", "api_key")):
        if source in updates:
            llm[target] = updates[source]
    if "llm_max_retries" in updates:
        llm["max_retries"] = updates["llm_max_retries"]
    if "llm_style" in updates:
        llm["style"] = updates["llm_style"]
    if llm:
        out["llm"] = llm
    if "embedding_enabled" in updates:
        out["embedding"] = {"enabled": updates["embedding_enabled"]}
    acp = {k.split("_", 1)[1]: updates[k]
           for k in ("acp_mode", "acp_command", "acp_args", "acp_cwd", "acp_timeout",
                     "acp_env_name", "acp_env_key") if k in updates}
    if acp:
        out["acp"] = {**out.get("acp", {}), **acp}
    return out


@router.put("/config")
async def update_config(request: Request, body: ConfigUpdateRequest):
    """热更新；persist 写入 YAML，persist_to_db 将面板设置写入 SQLite。"""
    agent = _agent(request)
    updates = body.model_dump(exclude_none=True, exclude={"persist", "persist_to_db"})
    for key in ("llm_base_url", "llm_model", "llm_api_key"):
        if key in updates:
            updates[key] = updates[key].strip()
    # 空 Key 表示不修改，不能把空值持久化后丢失已有凭据。
    if updates.get("llm_api_key") == "":
        updates.pop("llm_api_key")
    if body.persist_to_db and (body.persist or set(updates) - PANEL_SETTINGS_FIELDS.keys()):
        raise HTTPException(status_code=400, detail="SQLite 保存仅支持设置面板字段，且不能同时指定 persist")
    previous = panel_settings(agent.settings)
    try:
        agent.validate_config_updates(updates)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    persisted = False
    if body.persist:
        if not agent.settings.config_file:
            raise HTTPException(status_code=400, detail="未配置配置文件路径，无法持久化")
        try:
            save_config_partial(_config_yaml_updates(updates), agent.settings.config_file)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"配置文件写入失败: {exc}") from None
        persisted = True

    try:
        result = await agent.apply_config(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    if body.persist_to_db:
        db_settings = {
            key: value
            for key, value in panel_settings(agent.settings).items()
            if key in updates
        }
        try:
            request.app.state.db.save_settings(db_settings)
            persisted = True
        except sqlite3.Error:
            await agent.apply_config(previous)
            raise HTTPException(status_code=500, detail="SQLite 配置写入失败，设置未保存，请检查数据库后重试") from None

    return {
        "changed": result["changed"],
        "persisted": persisted,
        "config": mask_settings(agent.settings),
    }


@router.get("/config")
async def get_config(request: Request):
    return mask_settings(_agent(request).settings)


# ---- RAG 知识库 ----
@router.get("/knowledge")
async def knowledge_stats(request: Request, platform: str | None = None):
    """知识库状态：路径/文件数/分块数/最近扫描时间。

    传 platform=wechat/dingtalk 查看该平台生效知识库（独立目录优先，回退全局）；
    不传 platform 查看全局知识库。
    """
    agent = _agent(request)
    kb = agent.kb_for(_parse_platform(platform)) if platform else agent.kb
    return kb.stats()


@router.post("/knowledge/scan")
async def knowledge_rescan(request: Request, platform: str | None = None):
    """强制重扫知识库目录（改了文件后可手动触发；生成时也会自动检测）。"""
    agent = _agent(request)
    kb = agent.kb_for(_parse_platform(platform)) if platform else agent.kb
    return kb.scan(force=True)


def _system_roots() -> list[str]:
    """列出系统根目录。Windows 用 GetLogicalDrives 位掩码（零 I/O，逐个探测盘符会很慢）。"""
    if sys.platform == "win32":
        import ctypes

        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
        return [f"{chr(65 + i)}:\\" for i in range(26) if mask >> i & 1]
    return [str(Path.home())]


def _known_anchor(path: Path) -> bool:
    """盘符/根是否存在于系统根列表（未知盘符直接拒绝，避免阻塞式 is_dir 探测）。"""
    anchor = path.anchor
    if not anchor:
        return True  # 相对路径无 anchor，交由后续 is_dir 判断
    roots = _system_roots()
    return not roots or any(anchor.lower() == r.lower() for r in roots)


def _picker_script() -> str:
    """Windows 目录选择器脚本（现代资源管理器风格，置顶且居中显示）。"""
    csharp = '''
using System;
using System.Runtime.InteropServices;
public static class MoveDlg {
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindow(string cls, string title);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  public struct RECT { public int L; public int T; public int R; public int B; }
  public static bool CenterTo(string title, int cx, int cy) {
    IntPtr h = FindWindow("#32770", title);
    if (h == IntPtr.Zero || !IsWindowVisible(h)) return false;
    RECT r; GetWindowRect(h, out r);
    int w = r.R - r.L; int ht = r.B - r.T;
    SetWindowPos(h, IntPtr.Zero, cx - w/2, cy - ht/2, 0, 0, 0x0001 | 0x0004);
    return true;
  }
}
'''
    return (
        "$ErrorActionPreference = 'Stop'; "
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding; "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "Add-Type -TypeDefinition '" + csharp.replace("'", "''") + "'; "
        "$owner = New-Object System.Windows.Forms.Form; "
        "$owner.TopMost = $true; "
        "$owner.StartPosition = 'Manual'; "
        "$wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea; "
        "$owner.Location = New-Object System.Drawing.Point(($wa.X + [int](($wa.Width - 1) / 2)), ($wa.Y + [int](($wa.Height - 1) / 2))); "
        "$owner.Size = New-Object System.Drawing.Size(1, 1); "
        "$dialog = New-Object System.Windows.Forms.OpenFileDialog; "
        "$dialog.Title = '选择目录'; "
        "if ($env:CHATAGENT_PICKER_INITIAL -and "
        "(Test-Path -LiteralPath $env:CHATAGENT_PICKER_INITIAL -PathType Container)) "
        "{ $dialog.InitialDirectory = $env:CHATAGENT_PICKER_INITIAL }; "
        "$dialog.CheckFileExists = $false; "
        "$dialog.CheckPathExists = $true; "
        "$dialog.AddExtension = $false; "
        "$dialog.ValidateNames = $false; "
        "$dialog.Multiselect = $false; "
        "$dialog.RestoreDirectory = $true; "
        "$dialog.FileName = '选择此目录'; "
        "$dialog.Filter = '目录|*.directory'; "
        "$script:cx = [int]($wa.X + $wa.Width / 2); "
        "$script:cy = [int]($wa.Y + $wa.Height / 2); "
        "$script:tries = 0; "
        "$script:timer = New-Object System.Windows.Forms.Timer; "
        "$script:timer.Interval = 150; "
        "$script:timer.Add_Tick({ "
        "$script:tries++; "
        "if ([MoveDlg]::CenterTo('选择目录', $script:cx, $script:cy)) { $script:timer.Stop(); return }; "
        "if ($script:tries -gt 60) { $script:timer.Stop() } "
        "}); "
        "$script:timer.Start(); "
        "if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ $p = Split-Path -Parent $dialog.FileName; if ($p) { [Console]::Write($p) } }; "
        "$script:timer.Stop(); $script:timer.Dispose(); $owner.Dispose()"
    )


@router.post("/fs/pick-dir")
async def pick_directory(request: Request, path: str = Body('', embed=True)):
    """在运行服务的 Windows 桌面上打开系统目录选择器。"""
    from ..security import normalize_ip

    if not request.client or normalize_ip(request.client.host) not in ('127.0.0.1', '::1'):
        raise HTTPException(status_code=403, detail="系统目录选择器仅允许本机访问")
    if sys.platform != "win32":
        raise HTTPException(status_code=501, detail="原生目录选择器仅支持 Windows 本机服务")
    script = _picker_script()

    def show_dialog():
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={**os.environ, "CHATAGENT_PICKER_INITIAL": path},
            check=False,
        )

    try:
        result = await asyncio.to_thread(show_dialog)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=500, detail=f"无法打开目录选择器: {exc}") from None
    if result.returncode:
        detail = ((result.stderr or "") + (result.stdout or "")).strip()[-200:]
        raise HTTPException(status_code=500, detail=f"目录选择器启动失败: {detail or '未知错误'}")
    return {"path": result.stdout.strip()}


@router.get("/fs/dirs")
async def list_dirs(request: Request, path: str = ""):
    """列出目录下的子文件夹（前端目录选择器用）；path 为空时返回盘符根（Windows）。"""
    if not path.strip():
        return {"path": "", "parent": None, "dirs": _system_roots()}
    p = Path(path).expanduser()
    if not _known_anchor(p) or not p.is_dir():
        raise HTTPException(status_code=400, detail=f"目录不存在: {path}")
    dirs: list[str] = []
    try:
        for child in p.iterdir():
            try:
                if child.is_dir() and not child.name.startswith("."):
                    dirs.append(str(child))
            except OSError:  # noqa: PERF203 —— 个别系统目录无权限，跳过
                continue
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"无法读取目录: {exc}") from None
    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "dirs": sorted(dirs)}


@router.get("/messages")
async def list_messages(
    request: Request,
    platform: str | None = None,
    chat_id: str | None = None,
    direction: str | None = None,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
):
    msgs = _store(request).list_messages(
        platform=platform, chat_id=chat_id, direction=direction, status=status,
        limit=max(1, min(limit, 1000)), offset=max(0, offset)
    )
    return {"messages": [_message_payload(m) for m in msgs]}


@router.get("/replies")
async def list_replies(request: Request, status: str | None = None, limit: int = 100, offset: int = 0):
    replies = _store(request).replies(status=status, limit=max(1, min(limit, 500)), offset=max(0, offset))
    return {"replies": [_message_payload(m) for m in replies]}


@router.post("/replies/{reply_id}/approve")
async def approve_reply(reply_id: str, request: Request, body: ApproveRequest | None = None):
    """人工确认发送；body.content 可选，为编辑后的草稿内容（留空则发送原稿）。"""
    try:
        reply = await _agent(request).approve_reply(
            reply_id, content=body.content if body else None,
            confirm_unknown=body.confirm_unknown if body else False
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="回复不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"message": reply.model_dump(mode="json")}


@router.post("/replies/{reply_id}/select")
async def select_candidate(reply_id: str, request: Request, body: CandidateSelectRequest):
    """多候选切换：把第 index 条候选设为草稿正文。"""
    try:
        reply = await _agent(request).select_candidate(reply_id, body.index)
    except KeyError:
        raise HTTPException(status_code=404, detail="回复不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"message": reply.model_dump(mode="json")}


@router.post("/replies/{reply_id}/reject")
async def reject_reply(reply_id: str, request: Request):
    try:
        reply = await _agent(request).reject_reply(reply_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="回复不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"message": reply.model_dump(mode="json")}


@router.post("/simulate")
async def simulate(req: SimulateRequest, request: Request):
    """模拟一条微信/钉钉新消息，立即触发回复生成（开发/测试用）。"""
    agent = _agent(request)
    conn = agent.connectors.get(req.platform)
    if conn is None:
        raise HTTPException(status_code=400, detail=f"平台 {req.platform.value} 未启用")
    if not is_mock_connector(conn):
        raise HTTPException(
            status_code=400,
            detail=f"平台 {req.platform.value} 当前为 {conn.mode} 模式，模拟注入仅支持 mock 模式",
        )
    assert isinstance(conn, MockConnector)
    msg = await conn.inject(
        chat_id=req.chat_id, sender=req.sender, content=req.content,
        msg_type=req.msg_type, is_group=req.is_group, at_me=req.at_me,
    )
    await agent.ingest(msg)
    reply = next(
        (
            m
            for m in _store(request).list_messages(chat_id=req.chat_id, direction="out")
            if m.reply_to == msg.id
        ),
        None,
    )
    return {
        "message": msg.model_dump(mode="json"),
        "reply": reply.model_dump(mode="json") if reply else None,
    }


@router.post("/chat")
async def direct_chat(req: DirectChatRequest, request: Request):
    """前端“对话测试”：直接调用回复生成器接口。"""
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="内容不能为空")
    user_msg, reply = await _agent(request).direct_chat(req.content.strip(), req.chat_id, req.platform)
    return {
        "message": user_msg.model_dump(mode="json"),
        "reply": reply.model_dump(mode="json"),
    }


@router.post("/connectors/{platform}/start")
async def start_connector(platform: str, request: Request):
    agent = _agent(request)
    try:
        p = Platform(platform)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"未知平台 {platform}") from None
    if p not in agent.connectors:
        raise HTTPException(status_code=404, detail=f"平台 {platform} 未启用")
    try:
        conn = await agent.start_connector(p)
    except ConnectorStartError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"connector": conn.info()}


@router.post("/connectors/{platform}/stop")
async def stop_connector(platform: str, request: Request):
    agent = _agent(request)
    try:
        p = Platform(platform)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"未知平台 {platform}") from None
    if p not in agent.connectors:
        raise HTTPException(status_code=404, detail=f"平台 {platform} 未启用")
    conn = await agent.stop_connector(p)
    return {"connector": conn.info()}
