"""REST API 路由。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..config import mask_settings, save_config_partial
from ..connectors.base import ConnectorStartError
from ..connectors.mock import MockConnector, is_mock_connector
from ..models import (
    ApproveRequest,
    CandidateSelectRequest,
    ConfigUpdateRequest,
    ContactsRequest,
    DirectChatRequest,
    Platform,
    SimulateRequest,
)
from ..core.agent import AgentService
from ..persistence import SqliteStore

router = APIRouter(prefix="/api")

logger = logging.getLogger(__name__)


def _agent(request: Request) -> AgentService:
    return request.app.state.agent


def _store(request: Request):
    return request.app.state.store


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/status")
async def status(request: Request):
    agent = _agent(request)
    return {
        "engine": agent.engine.name,
        "jev": {
            "enabled": bool(agent.jev and agent.jev.enabled),
            "provider": getattr(agent.jev, "provider", None),
        },
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
    jev = {target: updates[source] for source, target in (("jev_enabled", "enabled"), ("jev_provider", "provider")) if source in updates}
    if jev:
        out["jev"] = jev
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
    """配置热更新：部分字段立即生效；persist=True 时写回本机配置文件。"""
    agent = _agent(request)
    updates = body.model_dump(exclude_none=True, exclude={"persist"})
    for key in ("llm_base_url", "llm_model", "llm_api_key"):
        if key in updates:
            updates[key] = updates[key].strip()
    # 空 Key 表示不修改，不能把空值持久化后丢失已有凭据。
    if updates.get("llm_api_key") == "":
        updates.pop("llm_api_key")
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
    return {"messages": [m.model_dump(mode="json") for m in msgs]}


@router.get("/replies")
async def list_replies(request: Request, status: str | None = None, limit: int = 100, offset: int = 0):
    replies = _store(request).replies(status=status, limit=max(1, min(limit, 500)), offset=max(0, offset))
    return {"replies": [m.model_dump(mode="json") for m in replies]}


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
