"""联系人白名单测试（Agent 策略 + REST API 运行时更新）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.agent import AgentService
from app.engine import MockReplyGenerator
from app.main import create_app
from app.models import ChatMessage, Platform, STATUS_IGNORED
from app.store import EventBus, MessageStore

from conftest import make_settings


def build_agent(settings):
    agent = AgentService(
        settings=settings, store=MessageStore(), bus=EventBus(), engine=MockReplyGenerator()
    )
    agent.setup_connectors()
    return agent


def make(platform=Platform.WECHAT, chat_id="c1", content="你好", **kw):
    return ChatMessage(platform=platform, chat_id=chat_id, sender="张三", content=content, **kw)


@pytest.mark.asyncio
async def test_empty_whitelist_denies_all():
    """默认不监听任何人：空名单时消息被忽略。"""
    agent = build_agent(make_settings(allowed_contacts=[]))
    await agent.ingest(make(chat_id="任何人"))
    msgs = agent.store.list_messages(chat_id="任何人")
    assert msgs and msgs[0].status == STATUS_IGNORED
    await agent.stop_all()


@pytest.mark.asyncio
async def test_whitelist_blocks_unlisted_chat():
    agent = build_agent(make_settings(allowed_contacts=["张三会话"]))
    # 不在名单 → 忽略
    await agent.ingest(make(chat_id="陌生会话"))
    assert agent.store.list_messages(chat_id="陌生会话", direction="out") == []
    m = agent.store.list_messages(chat_id="陌生会话")[0]
    assert m.status == STATUS_IGNORED
    assert "未选择监听" in m.meta["ignore_reason"]
    # 在名单 → 正常生成
    await agent.ingest(make(chat_id="张三会话"))
    assert len(agent.store.list_messages(chat_id="张三会话", direction="out")) == 1
    await agent.stop_all()


@pytest.mark.asyncio
async def test_whitelist_matches_meta_username():
    """微信 replica 的会话 username 存在 meta.replica.username，也应匹配。"""
    agent = build_agent(make_settings(allowed_contacts=["filehelper"]))
    m = make(chat_id="文件传输助手")
    m.meta = {"replica": {"username": "filehelper", "sender_id": "1", "sender_username": "", "raw_type": "文本"}}
    await agent.ingest(m)
    assert len(agent.store.list_messages(chat_id="文件传输助手", direction="out")) == 1
    await agent.stop_all()


@pytest.mark.asyncio
async def test_whitelist_wildcard():
    agent = build_agent(make_settings(allowed_contacts=["*"]))
    await agent.ingest(make(chat_id="谁都可以"))
    assert len(agent.store.list_messages(chat_id="谁都可以", direction="out")) == 1
    await agent.stop_all()


# ---------------- REST API 运行时更新 ----------------
def test_contacts_api_runtime_update():
    with TestClient(create_app(make_settings(allowed_contacts=[])), client=("127.0.0.1", 12345)) as client:
        # 默认空名单 → 不监听，消息被忽略
        r = client.post("/api/simulate", json={"platform": "dingtalk", "chat_id": "wl1", "content": "hi"})
        assert r.json()["reply"] is None

    # 设置白名单（不含 wl1）→ 新消息被忽略
        client.put("/api/contacts", json={"allowed_contacts": ["其他会话"]})
        assert client.get("/api/contacts").json()["allowed_contacts"] == ["其他会话"]
        r = client.post("/api/simulate", json={"platform": "dingtalk", "chat_id": "wl1", "content": "hi2"})
        assert r.json()["reply"] is None
        msgs = client.get("/api/messages", params={"chat_id": "wl1"}).json()["messages"]
        assert msgs[-1]["status"] == "ignored"

        # 加回 wl1 → 恢复生成
        client.put("/api/contacts", json={"allowed_contacts": ["wl1"]})
        r = client.post("/api/simulate", json={"platform": "dingtalk", "chat_id": "wl1", "content": "hi3"})
        assert r.json()["reply"] is not None


def test_contacts_persist_to_config_file(tmp_path):
        """白名单保存写回 config.yaml（全局 + 平台独立），重启后由 load_settings 恢复。"""
        import yaml as _yaml

        from app.config import load_settings

        cfg_file = tmp_path / "config.yaml"
        settings = make_settings(config_file=str(cfg_file))
        with TestClient(create_app(settings), client=("127.0.0.1", 12345)) as client:
            r = client.put("/api/contacts", json={"allowed_contacts": ["张三", "项目群"]})
            assert r.status_code == 200 and r.json()["persisted"] is True
            r2 = client.put("/api/contacts?platform=wechat", json={"allowed_contacts": ["文件传输助手"]})
            assert r2.status_code == 200 and r2.json()["persisted"] is True
            assert r2.json()["allowed_contacts"] == ["文件传输助手"]

        data = _yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert data["allowed_contacts"] == ["张三", "项目群"]
        assert data["wechat"]["allowed_contacts"] == ["文件传输助手"]

        loaded = load_settings(str(cfg_file))
        assert loaded.allowed_contacts == ["张三", "项目群"]
        assert loaded.wechat.allowed_contacts == ["文件传输助手"]


def test_contacts_no_config_file_still_applies_runtime():
        """未配置配置文件路径（单测默认）：仅改内存，响应 persisted=False。"""
        with TestClient(create_app(make_settings(config_file="")), client=("127.0.0.1", 12345)) as client:
            r = client.put("/api/contacts", json={"allowed_contacts": ["甲"]})
            assert r.json()["persisted"] is False
            assert client.get("/api/contacts").json()["allowed_contacts"] == ["甲"]
