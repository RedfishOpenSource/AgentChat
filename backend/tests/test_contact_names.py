"""联系人选择展示名称测试。

修复点：
- 微信 list_contacts 用 get_nickname() 解析显示名（get_sessions 不含昵称字段）
- 钉钉 dws list_contacts 调 `dws chat +chat-list` 返回（会话名, openConversationId）
- 白名单保存显示名后，_contact_allowed 经连接器把名称反解为 username 参与匹配
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.config import ConnectorSettings
from app.connectors.dingtalk import DingTalkDwsConnector
from app.connectors.wechat import WeChatReplicaConnector
from app.core.agent import AgentService
from app.engine import MockReplyGenerator
from app.main import create_app
from app.models import ChatMessage, Platform, STATUS_IGNORED
from app.store import EventBus, MessageStore

from conftest import make_settings


class FakeWeChatDB:
    """模拟 wechatauto.db.WeChatDB：get_sessions 无名称字段（与真实库一致）。"""

    def __init__(self):
        self.sessions = [
            {"username": "123@chatroom", "summary": "hi"},   # 群聊：contact.db 有群名
            {"username": "wxid_zhang", "summary": "yo"},     # 好友：有备注
            {"username": "gh_unknown", "summary": "x"},      # contact.db 未收录 → 回退 username
        ]

    def get_sessions(self, limit=100):
        return self.sessions[:limit]

    def get_nickname(self, user):
        names = {"123@chatroom": "项目群", "wxid_zhang": "张三（公司）"}
        return names.get(user, user)  # 未收录时原样返回 username（真实库行为）

    def username_by_nickname(self, name):
        return {"张三（公司）": "wxid_zhang", "项目群": "123@chatroom"}.get(name)


def make_wechat_connector() -> WeChatReplicaConnector:
    cs = ConnectorSettings(enabled=True, mode="replica")
    return WeChatReplicaConnector(cs, lambda msgs: None)


def build_agent(settings):
    agent = AgentService(
        settings=settings, store=MessageStore(), bus=EventBus(), engine=MockReplyGenerator()
    )
    agent.setup_connectors()
    return agent


# ---------------- 微信：会话列表解析显示名 ----------------


@pytest.mark.asyncio
async def test_wechat_list_contacts_resolves_names():
    """get_sessions 无名称字段时，用 get_nickname 解析；未收录的回退 username。"""
    conn = make_wechat_connector()
    conn._db = FakeWeChatDB()
    contacts = await conn.list_contacts()
    assert ("项目群", "123@chatroom") in contacts
    assert ("张三（公司）", "wxid_zhang") in contacts
    assert ("gh_unknown", "gh_unknown") in contacts  # 查不到名称 → 原样回退，不造假名


@pytest.mark.asyncio
async def test_wechat_list_contacts_without_db_returns_empty(monkeypatch):
    """微信未运行时惰性初始化 WeChatDB 失败 → 安全回退空列表，不抛异常。"""
    import wechatauto.db as wdb

    class _BrokenDB:
        def __init__(self):
            raise RuntimeError("微信客户端未登录")

    monkeypatch.setattr(wdb, "WeChatDB", _BrokenDB)
    conn = make_wechat_connector()  # 未启动（_db=None）
    assert await conn.list_contacts() == []
    assert conn._db is None  # 失败不缓存坏状态


@pytest.mark.asyncio
async def test_wechat_list_contacts_lazy_initializes_db(monkeypatch):
    """监听未启动（_db=None）时也能读会话列表：惰性创建 WeChatDB 并缓存。"""
    import wechatauto.db as wdb

    created = []

    class _LazyDB(FakeWeChatDB):
        def __init__(self):
            super().__init__()
            created.append(self)

    monkeypatch.setattr(wdb, "WeChatDB", _LazyDB)
    conn = make_wechat_connector()  # 未启动（_db=None）
    contacts = await conn.list_contacts()
    assert created, "应惰性创建 WeChatDB"
    assert conn._db is created[0]  # 缓存供后续 username_by_name 复用
    assert ("项目群", "123@chatroom") in contacts


@pytest.mark.asyncio
async def test_wechat_username_by_name_reverse_resolve():
    conn = make_wechat_connector()
    conn._db = FakeWeChatDB()
    assert conn.username_by_name("张三（公司）") == "wxid_zhang"
    assert conn.username_by_name("不存在") is None
    assert conn.username_by_name("") is None
    conn._db = None
    assert conn.username_by_name("张三") is None


# ---------------- 钉钉：假 CLI 会话列表 ----------------


@pytest.mark.asyncio
async def test_dingtalk_list_contacts_parses_fake_cli(tmp_path):
    fake = tmp_path / "fake-dws.bat"
    payload = json.dumps({
        "chats": [
            {"name": "调试群", "openConversationId": "cidA"},
            {"name": "", "openConversationId": "cidB"},      # 无名称 → 回退 cid
            {"name": "调试群", "openConversationId": "cidA"},  # 重复 cid → 去重
        ],
    }, ensure_ascii=False)
    # bat 的 echo 会把 JSON 里的逗号当参数分隔符，改用 type 原样输出文件字节
    json_file = tmp_path / "chats.json"
    json_file.write_text(payload, encoding="utf-8")
    bat = (
        "@echo off\r\n"
        'if "%1"=="chat" if "%2"=="+chat-list" (\r\n'
        f'  type "{json_file}"\r\n'
        "  exit /b 0\r\n"
        ")\r\n"
        "exit /b 1\r\n"
    )
    fake.write_text(bat, encoding="ascii")

    cs = ConnectorSettings(enabled=True, mode="dws", dws_bin=str(fake))
    conn = DingTalkDwsConnector(cs, lambda msgs: None)
    contacts = await conn.list_contacts()
    assert contacts == [("调试群", "cidA"), ("cidB", "cidB")]


@pytest.mark.asyncio
async def test_dingtalk_list_contacts_missing_cli_returns_empty(tmp_path):
    cs = ConnectorSettings(enabled=True, mode="dws", dws_bin=str(tmp_path / "no-dws.exe"))
    conn = DingTalkDwsConnector(cs, lambda msgs: None)
    assert await conn.list_contacts() == []


# ---------------- 白名单：显示名反解匹配 ----------------


class _ResolverConn:
    """带名称反解能力的连接器 stub（模拟微信 replica 行为）。"""

    platform = Platform.WECHAT
    status = "stopped"

    def __init__(self, mapping=None):
        self._mapping = mapping or {}

    def username_by_name(self, name):
        return self._mapping.get(name)

    def info(self):
        return {"platform": self.platform.value, "mode": "stub", "status": self.status, "last_error": ""}


def make(platform=Platform.WECHAT, chat_id="c1", **kw):
    return ChatMessage(platform=platform, chat_id=chat_id, sender="某人", content="你好", **kw)


@pytest.mark.asyncio
async def test_whitelist_display_name_matches_username():
    """白名单保存显示名「张三（公司）」，消息只带 username → 反解后匹配放行。"""
    s = make_settings()
    s.wechat = ConnectorSettings(enabled=True, mode="replica", allowed_contacts=["张三（公司）"])
    agent = build_agent(s)
    agent.connectors[Platform.WECHAT] = _ResolverConn({"张三（公司）": "wxid_zhang"})

    m = make(chat_id="昵称对不上", meta={"replica": {"username": "wxid_zhang"}})
    await agent.ingest(m)
    assert len(agent.store.list_messages(chat_id="昵称对不上", direction="out")) == 1
    await agent.stop_all()


@pytest.mark.asyncio
async def test_whitelist_unresolvable_name_still_ignored():
    """反解不出 username 的白名单条目不应放行消息。"""
    s = make_settings()
    s.wechat = ConnectorSettings(enabled=True, mode="replica", allowed_contacts=["陌生人"])
    agent = build_agent(s)
    agent.connectors[Platform.WECHAT] = _ResolverConn({})

    await agent.ingest(make(chat_id="路人会话", meta={"replica": {"username": "wxid_other"}}))
    msgs = agent.store.list_messages(chat_id="路人会话")
    assert msgs and msgs[0].status == STATUS_IGNORED
    await agent.stop_all()


# ---------------- suggestions API：来源标记与名称 ----------------


def test_suggestions_wechat_uses_session_names(tmp_path):
    """GET /api/contacts/suggestions 返回会话列表来源的显示名。"""
    s = make_settings()
    s.wechat = ConnectorSettings(enabled=True, mode="replica")
    app = create_app(s)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        conn = client.app.state.agent.connectors[Platform.WECHAT]
        conn._db = FakeWeChatDB()
        data = client.get("/api/contacts/suggestions", params={"platform": "wechat"}).json()
    by_username = {item["username"]: item for item in data["suggestions"]}
    assert by_username["123@chatroom"]["name"] == "项目群"
    assert by_username["wxid_zhang"]["name"] == "张三（公司）"
    assert by_username["123@chatroom"]["source"] == "会话列表"
