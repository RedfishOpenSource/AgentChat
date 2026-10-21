"""可插拔 Agent 接入测试（ACP 协议 / pi RPC 适配）。

用内存 Fake Agent 子进程验证完整协议链路，不依赖真实 Agent、不联网。
"""
from __future__ import annotations

import sys

import pytest

from app.config import load_settings
from tests.test_config_hot import hot_client

FAKE_ACP_AGENT = r'''
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
        text = "".join(b.get("text", "") for b in msg["params"].get("prompt", []) if isinstance(b, dict))
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid,
            "update": {"sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": f"[{sid}] echo:{text}"}}}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}}), flush=True)
'''

FAKE_PI_AGENT = r'''
import json, sys
argv = sys.argv[1:]
sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else "unknown"
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    t = msg.get("type")
    if t == "prompt":
        print(json.dumps({"id": msg.get("id"), "type": "response", "command": "prompt", "success": True}), flush=True)
        print(json.dumps({"type": "agent_settled"}), flush=True)
    elif t == "get_last_assistant_text":
        print(json.dumps({"id": msg.get("id"), "type": "response", "command": "get_last_assistant_text",
                          "success": True, "data": {"text": f"[{sid}] pi-echo"}}), flush=True)
'''

FAKE_ACP_RETRY_EMPTY = r'''
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
        for n in (1, 2, 3):
            print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": sid,
                "update": {"sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": f"Retrying (attempt {n}/3, waiting {2*n}s)..."}}}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid,
            "update": {"sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "Retry finished, resuming."}}}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}}), flush=True)
'''


def _configure(hot_client, payload: dict):
    r = hot_client.put("/api/config", json=payload)
    assert r.status_code == 200, r.text
    return r


def test_windows_command_wrapper_uses_cmd_exe(monkeypatch):
    import os
    import app.engine.acp as acp

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setattr(acp.shutil, "which", lambda command: r"C:\Agent Tools\pi-acp.CMD")
    argv = acp._command_argv("pi-acp", ["--mode", "rpc"])
    assert argv[:4] == [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c"]
    assert '"C:\\Agent Tools\\pi-acp.CMD"' in argv[4]
    assert argv[4].endswith("--mode rpc")


def test_acp_engine_session_per_contact(hot_client, tmp_path):
    """ACP 模式：握手 → 每个联系人独立会话，同一联系人复用会话。"""
    script = tmp_path / "fake_acp_agent.py"
    script.write_text(FAKE_ACP_AGENT, encoding="utf-8")
    _configure(hot_client, {"engine": "acp", "acp_mode": "acp",
                            "acp_command": sys.executable, "acp_args": str(script)})
    assert hot_client.get("/api/status").json()["engine"] == "acp"

    d1 = hot_client.post("/api/chat", json={"content": "你好", "chat_id": "c1"}).json()["reply"]
    assert d1["meta"]["engine"] == "acp"
    assert "sess-1" in d1["content"] and "你好" in d1["content"]
    d2 = hot_client.post("/api/chat", json={"content": "再次", "chat_id": "c1"}).json()["reply"]
    assert "sess-1" in d2["content"], "同一联系人应复用同一会话"
    d3 = hot_client.post("/api/chat", json={"content": "新会话", "chat_id": "c2"}).json()["reply"]
    assert "sess-2" in d3["content"], "不同联系人应新开会话"


def test_pi_engine_session_binding(hot_client, tmp_path):
    """pi 模式：同一联系人稳定绑定同一 --session-id，多轮上下文由 pi 会话文件保证。"""
    import re
    script = tmp_path / "fake_pi_agent.py"
    script.write_text(FAKE_PI_AGENT, encoding="utf-8")
    _configure(hot_client, {"engine": "acp", "acp_mode": "pi",
                            "acp_command": sys.executable, "acp_args": str(script)})
    d1 = hot_client.post("/api/chat", json={"content": "你好", "chat_id": "p1"}).json()["reply"]
    assert d1["meta"]["engine"] == "acp" and d1["meta"]["mode"] == "pi"
    m1 = re.search(r"\[chatagent-([0-9a-f]{16})\]", d1["content"])
    assert m1, d1["content"]
    d2 = hot_client.post("/api/chat", json={"content": "再次", "chat_id": "p1"}).json()["reply"]
    assert re.search(r"\[chatagent-([0-9a-f]{16})\]", d2["content"]).group(1) == m1.group(1)
    d3 = hot_client.post("/api/chat", json={"content": "别人", "chat_id": "p2"}).json()["reply"]
    assert re.search(r"\[chatagent-([0-9a-f]{16})\]", d3["content"]).group(1) != m1.group(1)


def test_acp_config_persist_roundtrip(hot_client, tmp_path):
    _configure(hot_client, {"engine": "acp", "acp_mode": "pi", "acp_command": "pi",
                            "acp_args": "-nt", "acp_cwd": str(tmp_path),
                            "acp_env_name": "PI_API_KEY", "acp_env_key": "test-key",
                            "acp_timeout": 240, "persist": True})
    cfg_file = hot_client.app.state.settings.config_file
    loaded = load_settings(cfg_file)
    assert loaded.acp.mode == "pi"
    assert loaded.acp.command == "pi"
    assert loaded.acp.args == "-nt"
    assert loaded.acp.cwd == str(tmp_path)
    assert loaded.acp.env_name == "PI_API_KEY"
    assert loaded.acp.env_key == "test-key"
    assert loaded.acp.timeout == 240


def test_acp_validation_rejects(hot_client, tmp_path):
    # engine=acp 但没有启动命令
    assert hot_client.put("/api/config", json={"engine": "acp"}).status_code == 400
    # 非法 mode
    assert hot_client.put("/api/config", json={"acp_mode": "nope"}).status_code == 400
    # 工作空间目录不存在
    assert hot_client.put("/api/config", json={"acp_cwd": str(tmp_path / "nope")}).status_code == 400
    # 超时范围
    assert hot_client.put("/api/config", json={"acp_timeout": 1}).status_code == 400


# ---------------- Agent 注册表（下拉选择） ----------------


def test_agents_endpoint_lists_builtin_with_availability(hot_client, monkeypatch):
    """/api/agents 返回内置注册表，available 按 PATH 检测（这里 mock 掉避免依赖真实环境）。"""
    import app.agents_registry as reg

    monkeypatch.setattr(
        reg.shutil, "which",
        lambda cmd: "C:/fake/" + cmd + ".cmd" if cmd in ("pi-acp", "claude-code-acp", "codex-acp", "pi") else None,
    )
    data = hot_client.get("/api/agents").json()
    agents = {a["id"]: a for a in data["agents"]}

    assert set(agents) == {"pi", "claude-code", "codex", "hermes"}
    assert agents["pi"]["available"] is True and agents["pi"]["command"] == "pi-acp"
    assert agents["claude-code"]["available"] is True
    assert agents["codex"]["available"] is True
    assert agents["hermes"]["available"] is False  # 未安装 → 标记不可用，但仍在下拉中可见


def test_agents_endpoint_real_path_detection(hot_client):
    """不做 mock：端点结构完整、available 为布尔（本机环境决定具体值）。"""
    data = hot_client.get("/api/agents").json()
    agents = data["agents"]
    assert len(agents) == 4
    for a in agents:
        assert isinstance(a["available"], bool)
        assert a["command"] and a["name"] and a["mode"] in ("acp", "pi")


@pytest.mark.asyncio
async def test_acp_empty_after_retries_raises_clear_error(tmp_path):
    """模型连续重试后仍无正文：明确报错，而不是沉默的「未返回文本内容」。"""
    from app.config import ACPSettings
    from app.engine.acp import ACPAgentError, ACPAgentGenerator
    from app.models import ChatMessage, Platform

    script = tmp_path / "fake_retry_agent.py"
    script.write_text(FAKE_ACP_RETRY_EMPTY, encoding="utf-8")
    engine = ACPAgentGenerator(ACPSettings(mode="acp", command=sys.executable,
                                           args=str(script), timeout=30))
    try:
        with pytest.raises(ACPAgentError, match="连续 3 次调用失败"):
            await engine.generate(ChatMessage(platform=Platform.WEB, chat_id="r1", content="hi"), [])
    finally:
        await engine.aclose()
