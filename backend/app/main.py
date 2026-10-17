"""FastAPI 应用入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .alerts import AlertManager, build_notifier
from .config import Settings, load_settings
from .core.agent import AgentService
from .engine import build_engine
from .persistence import SqliteStore
from .rag import KnowledgeBase
from .security import IPAllowlistMiddleware
from .store import EventBus, MessageStore

logger = logging.getLogger("chatagent")


def _setup_logging(log_file: str) -> None:
    """控制台 + 按天轮转文件日志（保留 14 天）；控制台仅配置一次，文件按需去重添加。"""
    root = logging.getLogger()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not getattr(root, "_chatagent_configured", False):
        root.setLevel(logging.INFO)
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
        root._chatagent_configured = True  # type: ignore[attr-defined]
    if log_file:
        try:
            resolved = str(Path(log_file).resolve())
            existing = {getattr(h, "baseFilename", None) for h in root.handlers}
            if resolved not in existing:
                Path(log_file).parent.mkdir(parents=True, exist_ok=True)
                fh = TimedRotatingFileHandler(log_file, when="midnight", backupCount=14, encoding="utf-8")
                fh.setFormatter(fmt)
                root.addHandler(fh)
        except OSError as exc:
            logging.warning("日志文件不可用(%s): %s", log_file, exc)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    _setup_logging(settings.log_file)
    db = SqliteStore(settings.db_path)
    store = MessageStore(max_size=settings.max_store, db=db)
    store.restore()  # 启动时恢复历史消息与未确认草稿
    bus = EventBus()
    kb = KnowledgeBase()
    if settings.knowledge_dir.strip():
        try:
            kb.configure(settings.knowledge_dir)
        except ValueError as exc:
            logger.warning("知识库目录不可用，RAG 未启用: %s", exc)
    engine, jev = build_engine(settings, kb=kb)
    alert_manager = AlertManager(
        notifier=build_notifier(settings.alert_channel, settings.alert_dingtalk_chat_id,
                                dws_bin=settings.dingtalk.dws_bin),
        min_interval=settings.alert_min_interval,
    )
    agent = AgentService(settings=settings, store=store, bus=bus, engine=engine, jev=jev,
                         alerts=alert_manager, kb=kb)
    agent.setup_connectors()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await agent.start_all()
        yield
        await agent.shutdown()
        db.close()

    app = FastAPI(
        title="ChatAgent",
        version="0.1.0",
        description="微信/钉钉智能回复助手：监听新消息 → 生成回复草稿 → 人工确认后发送（永不自动发送）",
        lifespan=lifespan,
    )
    # 访问控制：IP 白名单（无登录方案），同时作用于 HTTP 与 WebSocket
    app.add_middleware(IPAllowlistMiddleware, allowed_ips=settings.allowed_ips)
    app.state.settings = settings
    app.state.db = db
    app.state.store = store
    app.state.bus = bus
    app.state.agent = agent
    app.include_router(router)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        queue = bus.subscribe()
        try:
            await ws.send_json(
                {
                    "type": "hello",
                    "connectors": {p.value: c.info() for p, c in agent.connectors.items()},
                    "engine": agent.engine.name,
                }
            )
            while True:
                event = await queue.get()
                await ws.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(queue)

    # ---- 前端静态托管（生产模式：单进程交付）----
    static_dir = Path(settings.static_dir) if settings.static_dir else (
        Path(__file__).resolve().parents[2] / "frontend" / "dist"
    )
    if (static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="frontend")
        logger.info("前端静态资源已托管: %s", static_dir)
    else:
        logger.info("未找到前端构建产物(%s)，仅提供 API（开发请用 npm run dev）", static_dir)

    return app


app = create_app()
