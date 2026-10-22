"""Agent 配置（Studio）与联系人绑定：SQLite 持久化 + 按联系人路由引擎 + 提示词同步。"""
from __future__ import annotations

import asyncio
import os
import sys
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

        # 角色定义写入 .agents/memory/role.md；skills 后台安装；mcp 初始化
        role_file = _Path(ws) / ".agents" / "memory" / "role.md"
        assert role_file.is_file()
        assert "客服助手" in role_file.read_text(encoding="utf-8")
        installed = _Path(ws) / ".agents" / "skills" / "skill-report" / "SKILL.md"
        import time
        deadline = time.time() + 10
        while time.time() < deadline and not installed.is_file():
            time.sleep(0.1)
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
        # 未绑定的联系人回退第一个已创建的 Agent（工作目录以 Agent 配置为准，而非全局默认引擎）
        fallback = agent.profile_for(__import__("app.models", fromlist=["Platform"]).Platform.WECHAT, "其他人")
        assert fallback is not None and fallback["name"] == "客服助手"

        # 解绑后才能删除
        client.put("/api/agent-assignments", json={
            "platform": "wechat", "chat_id": "文件传输助手", "agent_id": "",
        })
        assert client.delete(f"/api/agent-profiles/{agent_id}").status_code == 200
        assert client.get("/api/agent-profiles").json()["profiles"] == []


@pytest.mark.parametrize("via_config", [False, True])
def test_remove_contact_unbinds_agent_and_clears_config(tmp_path, via_config):
    db_path = str(tmp_path / "removed.db")
    with _client(db_path) as client:
        agent = client.app.state.agent
        agent.save_agent_profile({"id": "a", "name": "A"})
        agent.save_agent_profile({"id": "b", "name": "B"})
        # 同名跨平台联系人、其他联系人均保留，不应连带解绑。
        for platform, chat_id, aid in [("wechat", "removed", "a"), ("wechat", "kept", "b"),
                                       ("dingtalk", "removed", "b")]:
            agent.assign_agent(platform, chat_id, aid)
            agent.save_contact_config(platform, chat_id, {"timeout": 60})
        client.put("/api/contacts?platform=wechat", json={"allowed_contacts": ["removed", "kept"]})
        assert client.delete("/api/agent-profiles/a").status_code == 400
        if via_config:
            response = client.put("/api/config", json={"wechat_allowed_contacts": ["kept"]})
        else:
            response = client.put("/api/contacts?platform=wechat", json={"allowed_contacts": ["kept"]})
        assert response.status_code == 200
        expected = [{"platform": "dingtalk", "chat_id": "removed", "agent_id": "b"},
                    {"platform": "wechat", "chat_id": "kept", "agent_id": "b"}]
        assert sorted(agent.agent_assignments_list(), key=lambda a: a["platform"]) == expected
        assert agent.db.list_agent_assignments() == expected
        assert ("wechat", "removed") not in agent.contact_configs
        assert "wechat:removed" not in agent.db.list_contact_configs()
        assert client.delete("/api/agent-profiles/a").status_code == 200
        assert client.delete("/api/agent-profiles/b").status_code == 400
    with _client(db_path) as client:
        data = client.get("/api/agent-profiles").json()
        assert [p["id"] for p in data["profiles"]] == ["b"]
        assert sorted(data["assignments"], key=lambda a: a["platform"]) == expected


def test_startup_repairs_legacy_unmonitored_assignments(tmp_path):
    from app.persistence import SqliteStore
    db_path = str(tmp_path / "legacy.db")
    db = SqliteStore(db_path)
    db.save_agent_profile({"id": "legacy", "name": "Legacy"})
    db.assign_agent("wechat", "removed", "legacy")
    db.save_contact_config("wechat", "removed", {"timeout": 60})
    db.close()
    app = create_app(make_settings(db_path=db_path, allowed_contacts=[]))
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        agent = app.state.agent
        assert agent.agent_assignments_list() == []
        assert agent.db.list_agent_assignments() == []
        assert agent.contact_configs == {}
        assert agent.db.list_contact_configs() == {}
        # 修复遗留绑定不删除 Agent，用户仍需明确点击删除。
        assert "legacy" in agent.agent_profiles
        assert client.delete("/api/agent-profiles/legacy").status_code == 200


def test_creation_prepares_worktree_and_interview_confirms_by_ai(tmp_path):
    """创建后先预建 worktree 再初始化问询；确认对话中由 AI 自主判定全部确认完毕后自动更新状态。"""
    import time

    fake_acp = r'''
import json, sys
sessions = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"protocolVersion": 1, "agentCapabilities": {}}}), flush=True)
    elif msg.get("method") == "session/new":
        sessions += 1
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"sessionId": f"sess-{sessions}"}}), flush=True)
    elif msg.get("method") == "session/prompt":
        sid = msg["params"]["sessionId"]
        text = f"[{sid}] 初始化确认清单：1. 项目路径 2. 目标分支"
        if "都确认" in json.dumps(msg, ensure_ascii=False):
            text += "\n【全部确认完毕】"
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid,
            "update": {"sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": text}}}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}}), flush=True)
'''
    db_path = str(tmp_path / "interview.db")
    ws = str(tmp_path / "ws")
    os.makedirs(ws, exist_ok=True)
    script = tmp_path / "fake_acp.py"
    script.write_text(fake_acp, encoding="utf-8")
    body = {"name": "问询", "description": "初始化问询验证", "engine": "pi",
            "command": sys.executable, "args": str(script), "workspace": ws,
            "capabilities": ["文件读写", "执行命令"],
            "sync_mode": "wait"}
    init_chat = None
    with _client(db_path) as client:
        r = client.post("/api/agent-profiles", json=body)
        assert r.status_code == 200, r.text
        # 保存即返回，Agent 立即可用（不设 pending 门禁）
        assert r.json()["profile"]["pending"] is False
        agent_id = r.json()["profile"]["id"]
        init_chat = f"debug:{agent_id}:init"
        agent = client.app.state.agent
        deadline = time.time() + 20
        while time.time() < deadline:
            interview = agent.agent_profiles[agent_id].get("init_interview") or {}
            if interview.get("status") in ("done", "failed"):
                break
            time.sleep(0.2)
        assert interview.get("status") == "done", interview
        # 问询不等润色、优先执行（问询只依赖预热+模板同步），随后预建 worktree，同一 init 会话内顺序执行
        history = client.get("/api/messages", params={
            "platform": "web", "chat_id": init_chat, "limit": 50}).json()["messages"]
        assert len(history) == 4
        assert "初始化确认" in history[0]["content"]
        assert "目标分支" in history[1]["content"]
        assert "worktree" in history[2]["content"]

        # 用户在 init 会话回复 → AI 判定全部确认完毕（回复携带标记）→ 状态自动更新为 confirmed
        r = client.post(f"/api/agent-profiles/{agent_id}/chat",
                        json={"session_id": "init", "content": "都确认，无补充"})
        assert r.status_code == 200
        assert agent.agent_profiles[agent_id]["init_interview"]["status"] == "confirmed"

        # 重新保存：已确认的问询不重跑，confirmed 状态保留；后台收尾会再次预建 worktree（+2 条）
        r = client.post("/api/agent-profiles", json={**body, "id": agent_id})
        assert r.status_code == 200
        iv, msgs = {}, []
        deadline = time.time() + 15
        while time.time() < deadline:
            iv = agent.agent_profiles[agent_id].get("init_interview") or {}
            msgs = client.get("/api/messages", params={
                "platform": "web", "chat_id": init_chat, "limit": 50}).json()["messages"]
            if len(msgs) >= 8 and iv.get("status") == "confirmed":
                break
            time.sleep(0.2)
        assert iv.get("status") == "confirmed" and len(msgs) == 8, (iv, len(msgs))

        # 失败的问询随重新保存自动重试（重试前会再次预建 worktree）
        profile = agent.agent_profiles[agent_id]
        profile["init_interview"] = {"status": "failed", "error": "模拟失败"}
        agent.save_agent_profile(profile)
        r = client.post("/api/agent-profiles", json={**body, "id": agent_id})
        assert r.status_code == 200
        deadline = time.time() + 30
        while time.time() < deadline:
            iv = agent.agent_profiles[agent_id].get("init_interview") or {}
            msgs = client.get("/api/messages", params={
                "platform": "web", "chat_id": init_chat, "limit": 50}).json()["messages"]
            if len(msgs) >= 12 and iv.get("status") in ("done", "failed"):
                break
            time.sleep(0.2)
        assert iv.get("status") == "done", iv
        assert len(msgs) == 12

    # 重启后：旧数据（done+answered）读取时迁移为 confirmed；running 恢复为失败并提示重试
    from app.persistence import SqliteStore
    db = SqliteStore(db_path)
    stored = db.list_agent_profiles()[0]
    stored["init_interview"] = {"status": "done", "answered": True}
    db.save_agent_profile(stored)
    db.close()
    with _client(db_path) as client:
        data = client.get("/api/agent-profiles").json()["profiles"][0]
        assert data["init_interview"]["status"] == "confirmed"
    db = SqliteStore(db_path)
    stored = db.list_agent_profiles()[0]
    stored["init_interview"] = {"status": "running"}
    db.save_agent_profile(stored)
    db.close()
    with _client(db_path) as client:
        data = client.get("/api/agent-profiles").json()["profiles"][0]
        assert data["init_interview"]["status"] == "failed"
        assert "重启" in data["init_interview"]["error"]


def test_profile_save_warms_agent_process_and_init_session(tmp_path):
    """保存 Agent 后并行预热：进程提前启动、init 调试会话预建，问询直接复用不冷启动。"""
    import time

    fake_acp = r'''
import json, sys
sessions = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"protocolVersion": 1, "agentCapabilities": {}}}), flush=True)
    elif msg.get("method") == "session/new":
        sessions += 1
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"sessionId": f"sess-{sessions}"}}), flush=True)
    elif msg.get("method") == "session/prompt":
        sid = msg["params"]["sessionId"]
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid,
            "update": {"sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": f"[{sid}] 初始化确认：无待确认内容"}}}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}}), flush=True)
'''
    db_path = str(tmp_path / "warmup.db")
    ws = str(tmp_path / "ws")
    os.makedirs(ws, exist_ok=True)
    script = tmp_path / "fake_acp.py"
    script.write_text(fake_acp, encoding="utf-8")
    body = {"name": "预热", "description": "预热验证", "engine": "pi",
            "command": sys.executable, "args": str(script), "workspace": ws,
            "sync_mode": "wait"}
    with _client(db_path) as client:
        r = client.post("/api/agent-profiles", json=body)
        assert r.status_code == 200, r.text
        agent_id = r.json()["profile"]["id"]
        agent = client.app.state.agent
        key = ("web", f"debug:{agent_id}:init")
        deadline = time.time() + 15
        while time.time() < deadline:
            engine = agent.profile_engines.get(agent_id)
            if engine is not None and engine._proc is not None \
                    and engine._proc.returncode is None and engine._sessions.get(key):
                break
            time.sleep(0.1)
        assert engine is not None, "保存后应后台预热引擎"
        assert engine._proc.returncode is None, "预热进程应存活"
        assert engine._sessions.get(key) == "sess-1", "init 调试会话应被预建"

        # 初始化问询复用预热会话（sess-1），不再新建会话
        deadline = time.time() + 20
        while time.time() < deadline:
            iv = agent.agent_profiles[agent_id].get("init_interview") or {}
            if iv.get("status") in ("done", "failed"):
                break
            time.sleep(0.2)
        assert iv.get("status") == "done", iv
        assert agent.profile_engines[agent_id]._sessions.get(key) == "sess-1", \
            "问询应复用预热会话，而不是冷启动后新建"


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


def test_delete_profile_while_replying_does_not_resurrect(tmp_path):
    """回复生成期间删除 Agent：回复完成后的状态保存不得复活已删除的 Agent。"""
    import threading
    import time

    gate = tmp_path / "gate.flag"

    slow_acp = r"""
import json, sys, time, os
sessions = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"protocolVersion": 1, "agentCapabilities": {}}}), flush=True)
    elif msg.get("method") == "session/new":
        sessions += 1
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"sessionId": f"sess-{sessions}"}}), flush=True)
    elif msg.get("method") == "session/prompt":
        sid = msg["params"]["sessionId"]
        if sid != "sess-1":  # 初始化问询立即回复；后续会话等门控文件，制造“回复生成中”的确定窗口
            while not os.path.exists(r"GATE"):
                time.sleep(0.05)
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid,
            "update": {"sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": f"[{sid}] done"}}}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}}), flush=True)
""".replace("GATE", str(gate))

    ws = str(tmp_path / "ws")
    os.makedirs(ws, exist_ok=True)
    script = tmp_path / "gate_acp.py"
    script.write_text(slow_acp, encoding="utf-8")
    body = {"name": "复活验证", "description": "删除与回复并发", "engine": "pi",
            "command": sys.executable, "args": str(script), "workspace": ws,
            "sync_mode": "wait"}
    with _client(str(tmp_path / "resurrect.db")) as client:
        r = client.post("/api/agent-profiles", json=body)
        assert r.status_code == 200, r.text
        agent_id = r.json()["profile"]["id"]
        chat_id = f"debug:{agent_id}:s1"
        agent = client.app.state.agent
        deadline = time.time() + 20
        while time.time() < deadline:
            iv = agent.agent_profiles[agent_id].get("init_interview") or {}
            if iv.get("status") in ("done", "failed"):
                break
            time.sleep(0.2)
        assert iv.get("status") == "done", iv

        result = {}

        def _reply():
            try:
                result["response"] = client.post(f"/api/agent-profiles/{agent_id}/chat",
                                                 json={"session_id": "s1", "content": "确认"})
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        worker = threading.Thread(target=_reply)
        worker.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if agent.store.list_messages(platform="web", chat_id=chat_id, direction="in"):
                break
            time.sleep(0.05)
        # 回复仍在生成中（脚本阻塞在门控文件上），此时删除 Agent
        agent.prune_removed_contacts()
        agent.delete_agent_profile(agent_id)
        gate.write_text("go", encoding="utf-8")
        worker.join(20)
        assert not worker.is_alive(), result
        assert "error" not in result, result
        assert agent_id not in agent.agent_profiles
        assert agent.db.list_agent_profiles() == []
