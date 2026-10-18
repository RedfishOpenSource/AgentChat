"""REST API 路由。"""
from __future__ import annotations

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
        "allowed_contacts": agent.settings.allowed_contacts,
        "alerts": agent.alerts.active_list(),
        "connectors": {p.value: c.info() for p, c in agent.connectors.items()},
        "stats": {"messages": _store(request).count()},
    }


@router.get("/audit")
async def list_audit(request: Request, limit: int = 50):
    """审计日志（最近的发送/驳回操作）。"""
    db: SqliteStore | None = getattr(request.app.state, "db", None)
    if db is None:
        return {"audit": []}
    return {"audit": db.list_audit(limit=max(1, min(limit, 500)))}


@router.get("/contacts")
async def get_contacts(request: Request):
    """联系人白名单（空 = 允许全部）。"""
    return {"allowed_contacts": _agent(request).settings.allowed_contacts}


@router.put("/contacts")
async def set_contacts(request: Request, body: ContactsRequest):
    """运行时更新联系人白名单（立即生效；持久化请在 config.yaml 中同步修改）。"""
    agent = _agent(request)
    agent.settings.allowed_contacts = [x.strip() for x in body.allowed_contacts if str(x).strip()]
    return {"allowed_contacts": agent.settings.allowed_contacts}


@router.put("/config")
async def update_config(request: Request, body: ConfigUpdateRequest):
    """配置热更新：部分字段立即生效；persist=True 时写回配置文件（jev_api_key 不落盘）。"""
    agent = _agent(request)
    updates = body.model_dump(exclude_none=True, exclude={"persist"})
    try:
        result = await agent.apply_config(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    persisted = False
    if body.persist:
        if not agent.settings.config_file:
            raise HTTPException(status_code=400, detail="未配置配置文件路径，无法持久化")
        yaml_updates: dict = {}
        if "engine" in updates:
            yaml_updates["engine"] = updates["engine"]
        jev_keys = {k: updates[k] for k in ("jev_enabled", "jev_provider") if k in updates}
        if jev_keys:
            yaml_updates["jev"] = jev_keys  # jev_api_key 不写盘
        llm_keys = {k: updates[k] for k in ("num_candidates", "temperature", "llm_max_retries") if k in updates}
        if "llm_style" in updates:
            llm_keys["style"] = updates["llm_style"]
        if llm_keys:
            yaml_updates["llm"] = llm_keys
        if "embedding_enabled" in updates:
            yaml_updates["embedding"] = {"enabled": updates["embedding_enabled"]}
        for k in ("group_trigger", "non_text_action", "debounce_seconds",
                  "llm_rate_limit_per_chat", "alert_channel", "alert_dingtalk_chat_id",
                  "allowed_contacts", "knowledge_dir"):
            if k in updates:
                yaml_updates[k] = updates[k]
        save_config_partial(yaml_updates, agent.settings.config_file)
        persisted = True

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
async def knowledge_stats(request: Request):
    """知识库状态：路径/文件数/分块数/最近扫描时间。"""
    return _agent(request).kb.stats()


@router.post("/knowledge/scan")
async def knowledge_rescan(request: Request):
    """强制重扫知识库目录（改了文件后可手动触发；生成时也会自动检测）。"""
    return _agent(request).kb.scan(force=True)


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
    limit: int = 200,
):
    msgs = _store(request).list_messages(
        platform=platform, chat_id=chat_id, direction=direction, limit=max(1, min(limit, 1000))
    )
    return {"messages": [m.model_dump(mode="json") for m in msgs]}


@router.get("/replies")
async def list_replies(request: Request, status: str | None = None, limit: int = 100):
    replies = _store(request).replies(status=status, limit=max(1, min(limit, 500)))
    return {"replies": [m.model_dump(mode="json") for m in replies]}


@router.post("/replies/{reply_id}/approve")
async def approve_reply(reply_id: str, request: Request, body: ApproveRequest | None = None):
    """人工确认发送；body.content 可选，为编辑后的草稿内容（留空则发送原稿）。"""
    try:
        reply = await _agent(request).approve_reply(
            reply_id, content=body.content if body else None
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
    msg = await conn.inject(chat_id=req.chat_id, sender=req.sender, content=req.content)
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
    user_msg, reply = await _agent(request).direct_chat(req.content.strip(), req.chat_id)
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
