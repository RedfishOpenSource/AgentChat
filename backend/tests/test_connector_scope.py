"""连接器级独立白名单 / 知识库测试。

每个平台（wechat / dingtalk）可单独配置 allowed_contacts 与 knowledge_dir；
未单独配置时回退全局默认。API 侧：
- GET/PUT /api/contacts?platform=...
- PUT /api/config {wechat_knowledge_dir|dingtalk_knowledge_dir|...}（persist 映射到 wechat:/dingtalk: 节点）
- GET /api/knowledge?platform=...
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import ConnectorSettings, load_settings
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


# ---------- 白名单 ----------


@pytest.mark.asyncio
async def test_platform_whitelist_overrides_global():
    """微信独立名单生效；钉钉未配置回退全局名单。"""
    s = make_settings(allowed_contacts=["全局联系人"])
    s.wechat = ConnectorSettings(enabled=True, mode="mock", allowed_contacts=["张三"])
    agent = build_agent(s)

    assert agent.effective_contacts(Platform.WECHAT) == ["张三"]
    assert agent.effective_contacts(Platform.DINGTALK) == ["全局联系人"]

    # 微信：独立名单内 → 正常；名单外 → 忽略
    await agent.ingest(make(platform=Platform.WECHAT, chat_id="张三"))
    assert len(agent.store.list_messages(platform="wechat", chat_id="张三", direction="out")) == 1
    await agent.ingest(make(platform=Platform.WECHAT, chat_id="全局联系人"))
    m = agent.store.list_messages(platform="wechat", chat_id="全局联系人")[0]
    assert m.status == STATUS_IGNORED

    # 钉钉：回退全局名单
    await agent.ingest(make(platform=Platform.DINGTALK, chat_id="全局联系人"))
    assert len(agent.store.list_messages(platform="dingtalk", chat_id="全局联系人", direction="out")) == 1
    await agent.stop_all()


@pytest.mark.asyncio
async def test_platform_whitelist_api_scoped(client: TestClient):
    """PUT /api/contacts?platform=wechat 只改微信；钉钉与全局不受影响。"""
    r = client.put("/api/contacts", json={"allowed_contacts": ["张三"]}, params={"platform": "wechat"})
    assert r.status_code == 200
    assert r.json()["platform"] == "wechat"
    assert r.json()["allowed_contacts"] == ["张三"]
    assert r.json()["persisted"] is False  # 测试夹具无配置文件路径，仅内存生效

    st = client.get("/api/status").json()
    assert st["allowed_contacts"]["wechat"] == ["张三"]
    assert st["allowed_contacts"]["dingtalk"] == ["*"]  # 钉钉不受影响，回退全局
    assert st["allowed_contacts_global"] == ["*"]  # 全局不受影响

    # 不带 platform 仍是全局语义（兼容旧客户端）
    client.put("/api/contacts", json={"allowed_contacts": ["李四"]})
    st = client.get("/api/status").json()
    assert st["allowed_contacts_global"] == ["李四"]
    assert st["allowed_contacts"]["wechat"] == ["张三"]  # 微信独立名单优先
    assert st["allowed_contacts"]["dingtalk"] == ["李四"]  # 钉钉回退全局


# ---------- 知识库 ----------


def _make_kb_dir(tmp_path, name: str):
    d = tmp_path / name
    d.mkdir()
    (d / "note.md").write_text(f"# {name}\n\n{name} 的参考资料内容。", encoding="utf-8")
    return d


def test_platform_knowledge_dir_hot_update(client: TestClient, tmp_path):
    """微信配置独立知识库；钉钉回退全局；清空微信目录后回退全局。"""
    d_wechat = _make_kb_dir(tmp_path, "kb_wechat")
    d_global = _make_kb_dir(tmp_path, "kb_global")

    r = client.put("/api/config", json={"wechat_knowledge_dir": str(d_wechat)})
    assert r.status_code == 200 and "wechat_knowledge_dir" in r.json()["changed"]
    r = client.put("/api/config", json={"knowledge_dir": str(d_global)})
    assert r.status_code == 200

    st_wechat = client.get("/api/knowledge", params={"platform": "wechat"}).json()
    st_dingtalk = client.get("/api/knowledge", params={"platform": "dingtalk"}).json()
    st_global = client.get("/api/knowledge").json()
    assert st_wechat["path"] == str(d_wechat)
    assert st_dingtalk["path"] == str(d_global)
    assert st_global["path"] == str(d_global)

    # 清空微信独立目录 → 回退全局库
    r = client.put("/api/config", json={"wechat_knowledge_dir": ""})
    assert r.status_code == 200
    st_wechat = client.get("/api/knowledge", params={"platform": "wechat"}).json()
    assert st_wechat["path"] == str(d_global)

    # 不存在的目录 → 400
    assert client.put("/api/config", json={"wechat_knowledge_dir": str(tmp_path / "no")}).status_code == 400


def test_platform_knowledge_dir_persist_mapping(tmp_path):
    """persist 时连接器级字段写入 config.yaml 的 wechat:/dingtalk: 子节点。"""
    d = _make_kb_dir(tmp_path, "kb_persist")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("wechat:\n  mode: mock\ndingtalk:\n  mode: mock\n", encoding="utf-8")

    s = make_settings(config_file=str(cfg_file))
    with TestClient(create_app(s), client=("127.0.0.1", 12345)) as client:
        r = client.put(
            "/api/config",
            json={
                "wechat_allowed_contacts": ["张三"],
                "wechat_knowledge_dir": str(d),
                "dingtalk_allowed_contacts": ["项目群"],
                "persist": True,
            },
        )
        assert r.status_code == 200 and r.json()["persisted"] is True

    s = load_settings(str(cfg_file))
    assert s.wechat.allowed_contacts == ["张三"]
    assert s.wechat.knowledge_dir == str(d)
    assert s.dingtalk.allowed_contacts == ["项目群"]
    assert s.dingtalk.knowledge_dir == ""


@pytest.mark.asyncio
async def test_agent_uses_platform_kb_instance(tmp_path):
    """配置了独立目录的平台使用专属 KB 实例；未配置的平台回退全局实例。"""
    s = make_settings()
    s.wechat = ConnectorSettings(enabled=True, mode="mock", knowledge_dir=str(_make_kb_dir(tmp_path, "kb_w")))
    agent = build_agent(s)

    assert agent.kb_for(Platform.WECHAT) is not agent.kb
    assert agent.kb_for(Platform.DINGTALK) is agent.kb
    assert agent.kb_for(Platform.WECHAT).stats()["path"] == s.wechat.knowledge_dir
    await agent.stop_all()
