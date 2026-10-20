"""Agent 配置（Studio）与联系人绑定：SQLite 持久化 + 按联系人路由引擎 + 提示词同步。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path as _Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings


@pytest.fixture(autouse=True)
def _fast_sync(monkeypatch):
    """除专项同步测试外，替换掉真实的 GitHub 拉取（避免测试联网/耗时）。"""
    import app.prompts_sync as ps

    monkeypatch.setattr(
        ps, "_real_sync",
        ps.sync_workspace_prompts, raising=False,
    )

    async def fake(workspace, transport=None, prefer_git=True, attempts=3):
        return {"synced": 0, "files": [], "target": workspace, "via": "mock"}

    monkeypatch.setattr(ps, "sync_workspace_prompts", fake)
    async def rewrite(settings, description):
        return f"优化后的角色：{description}。按职责提供专业简洁的帮助。"
    monkeypatch.setattr("app.api.routes._llm_role_prompt", rewrite)
    yield


def _client(db_path):
    return TestClient(create_app(make_settings(db_path=db_path)), client=("127.0.0.1", 12345))


def test_agent_profile_crud_and_assignment_persist(tmp_path):
    db_path = str(tmp_path / "agents.db")
    ws = str(tmp_path / "ws")
    os.makedirs(ws, exist_ok=True)
    skill_src = tmp_path / "skill-report"
    skill_src.mkdir()
    (skill_src / "SKILL.md").write_text("# 报销问答\n", encoding="utf-8")

    with _client(db_path) as client:
        r = client.post("/api/agent-profiles", json={
            "name": "客服助手", "description": "售后问答",
            "instructions": "用中文简短回复", "skills": [str(skill_src)],
            "engine": "pi", "workspace": ws, "capabilities": ["shell"],
            "knowledge_dir": "", "mcp_servers": [], "plan_before_execute": True,
            "timeout": 120, "sync_mode": "wait",
        })
        assert r.status_code == 200, r.text
        agent_id = r.json()["profile"]["id"]
        assert r.json()["profile"]["command"] == "pi-acp"

        r = client.put("/api/agent-assignments", json={
            "platform": "wechat", "chat_id": "文件传输助手", "agent_id": agent_id,
        })
        assert r.status_code == 200

        data = client.get("/api/agent-profiles").json()
        assert data["assignments"] == [
            {"platform": "wechat", "chat_id": "文件传输助手", "agent_id": agent_id}
        ]

        # 被 监听联系人 绑定的 Agent 不能直接删除
        r = client.delete(f"/api/agent-profiles/{agent_id}")
        assert r.status_code == 400
        assert "正在被监听联系人使用" in r.json()["detail"]

        # 角色定义写入 .agents/memory/role.md；skills 安装；mcp 初始化
        role_file = _Path(ws) / ".agents" / "memory" / "role.md"
        assert role_file.is_file()
        assert "客服助手" in role_file.read_text(encoding="utf-8")
        installed = _Path(ws) / ".agents" / "skills" / "skill-report" / "SKILL.md"
        assert installed.is_file()
        mcp_file = _Path(ws) / ".agents" / "mcps" / ".mcp.json"
        assert mcp_file.is_file() and "mcpServers" in mcp_file.read_text(encoding="utf-8")

    # 重启后配置与绑定仍在
    with _client(db_path) as client:
        data = client.get("/api/agent-profiles").json()
        assert [p["name"] for p in data["profiles"]] == ["客服助手"]
        assert data["assignments"][0]["agent_id"]

        agent = client.app.state.agent
        profile = agent.profile_for(__import__("app.models", fromlist=["Platform"]).Platform.WECHAT, "文件传输助手")
        assert profile is not None and profile["name"] == "客服助手"
        # 未绑定的联系人回退默认引擎
        assert agent.profile_for(__import__("app.models", fromlist=["Platform"]).Platform.WECHAT, "其他人") is None

        # 解绑后才能删除
        client.put("/api/agent-assignments", json={
            "platform": "wechat", "chat_id": "文件传输助手", "agent_id": "",
        })
        assert client.delete(f"/api/agent-profiles/{agent_id}").status_code == 200
        assert client.get("/api/agent-profiles").json()["profiles"] == []


def test_agent_profile_rejects_missing_workspace(tmp_path):
    with _client(str(tmp_path / "x.db")) as client:
        r = client.post("/api/agent-profiles", json={
            "name": "A", "engine": "pi", "workspace": str(tmp_path / "nope"),
        })
        assert r.status_code == 400


def test_prompts_sync_downloads_and_overwrites(tmp_path):
    """提示词语案例同步：GitHub 目录列举 → 逐文件下载 → 覆盖写入工作空间。"""
    import httpx

    import app.prompts_sync as ps
    sync_workspace_prompts = ps._real_sync

    ws = tmp_path / "ws"
    ws.mkdir()
    prompts = ws / ".agents" / "memory" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "keep.md").write_text("keep", encoding="utf-8")  # 远端没有的本地文件

    def handler(request: "httpx.Request") -> "httpx.Response":
        if request.url.host == "api.github.com":
            raw_path = request.url.path
            if raw_path.endswith("/sub"):
                return httpx.Response(200, json=[])  # 空子目录
            assert "02.提示词案例" in raw_path, raw_path
            return httpx.Response(200, json=[
                {"type": "file", "name": "case1.md", "path": "04.Agent开发/04.记忆与自进化/02.提示词案例/case1.md",
                 "download_url": "https://raw.test/case1.md"},
                {"type": "file", "name": "stale.md", "path": "04.Agent开发/04.记忆与自进化/02.提示词案例/stale.md",
                 "download_url": "https://raw.test/stale.md"},
                {"type": "dir", "name": "sub", "path": "04.Agent开发/04.记忆与自进化/02.提示词案例/sub",
                 "download_url": None},
            ])
        name = str(request.url).rsplit("/", 1)[-1]
        return httpx.Response(200, content=f"# 新版 {name}".encode("utf-8"))

    async def call():
        return await sync_workspace_prompts(str(ws), transport=httpx.MockTransport(handler), prefer_git=False)

    result = asyncio.run(call())
    assert result["synced"] == 2
    assert result["files"] == ["case1.md", "stale.md"]
    # 顶层文件按相对路径映射到工作空间根目录
    assert (ws / "stale.md").read_text(encoding="utf-8") == "# 新版 stale.md"
    assert (ws / "case1.md").read_text(encoding="utf-8") == "# 新版 case1.md"
    # 远端没有的本地文件保持不动（覆盖语义，不做清理）
    assert (prompts / "keep.md").read_text(encoding="utf-8") == "keep"


def test_prompts_sync_rejects_missing_workspace(tmp_path):
    import app.prompts_sync as ps

    async def call():
        await ps._real_sync(str(tmp_path / "nope"))

    try:
        asyncio.run(call())
    except ValueError as exc:
        assert "工作空间" in str(exc)
    else:
        raise AssertionError("should raise ValueError")


def test_contact_config_persist_and_apply(tmp_path):
    """监听联系人的逐人设置：持久化 + 运行时覆盖全局默认。"""
    db_path = str(tmp_path / "cc.db")
    with _client(db_path) as client:
        r = client.put("/api/contact-configs", json={
            "platform": "wechat", "chat_id": "张三",
            "timeout": 60, "group_trigger": "all",
            "non_text_action": "skip", "debounce_seconds": 1,
        })
        assert r.status_code == 200
        data = client.get("/api/contact-configs", params={"platform": "wechat"}).json()
        assert data["configs"][0]["chat_id"] == "张三"
        assert data["configs"][0]["group_trigger"] == "all"
        assert client.put("/api/contact-configs", json={
            "platform": "wechat", "chat_id": "李四", "timeout": 1,
        }).status_code == 400

    with _client(db_path) as client:
        agent = client.app.state.agent
        cfg = agent.contact_config_for(
            __import__("app.models", fromlist=["Platform"]).Platform.WECHAT, "张三"
        )
        assert cfg["group_trigger"] == "all"
        assert cfg["non_text_action"] == "skip"
        assert cfg["debounce_seconds"] == 1
        # 未配置的联系人回退全局默认
        other = agent.contact_config_for(
            __import__("app.models", fromlist=["Platform"]).Platform.WECHAT, "王五"
        )
        assert other["group_trigger"] == "at-mention"
