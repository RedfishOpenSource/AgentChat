"""清空联系人会话：ACP 丢弃 sessionId、pi 模式更换 session-id、REST 入口校验。"""
from __future__ import annotations

import json
import sys

import pytest
from fastapi.testclient import TestClient

from app.engine.acp import ACPAgentGenerator
from app.main import create_app
from tests.conftest import make_settings

FAKE_ACP = r'''
import json, sys
sessions = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1}}), flush=True)
    elif msg.get("method") == "session/new":
        sessions += 1
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "result": {"sessionId": f"sess-{sessions}"}}), flush=True)
    elif msg.get("method") == "session/prompt":
        sid = msg["params"]["sessionId"]
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": sid}}}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}}), flush=True)
'''

FAKE_PI = r'''
import json, sys
argv = sys.argv[1:]
sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else "unknown"
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if msg.get("type") == "prompt":
        print(json.dumps({"id": msg.get("id"), "type": "response", "command": "prompt", "success": True}), flush=True)
        print(json.dumps({"type": "agent_settled"}), flush=True)
    elif msg.get("type") == "get_last_assistant_text":
        print(json.dumps({"id": msg.get("id"), "type": "response", "command": "get_last_assistant_text",
                          "success": True, "data": {"text": sid}}), flush=True)
'''


def _client(db_path):
    return TestClient(create_app(make_settings(db_path=db_path, allowed_contacts=["*"])),
                      client=("127.0.0.1", 12345))

def _configure(client, tmp_path, script, mode):
    script_file = tmp_path / f"fake_{mode}.py"
    script_file.write_text(script, encoding="utf-8")
    r = client.put("/api/config", json={
        "engine": "acp", "acp_mode": mode, "acp_command": sys.executable,
        "acp_args": str(script_file), "acp_timeout": 60,
    })
    assert r.status_code == 200, r.text


def _chat(client, chat_id, content="hi"):
    return client.post("/api/chat", json={"content": content, "chat_id": chat_id}).json()["reply"]["content"]


def _simulate(client, chat_id, content="hi"):
    r = client.post("/api/simulate", json={"platform": "wechat", "chat_id": chat_id,
                                           "content": content})
    assert r.status_code == 200, r.text
    return r.json()["reply"]["content"]


def test_reset_acp_session_starts_fresh(tmp_path):
    with _client(str(tmp_path / "a.db")) as client:
        _configure(client, tmp_path, FAKE_ACP, "acp")
        assert "sess-1" in _simulate(client, "c1")
        assert "sess-1" in _simulate(client, "c1")  # 同联系人复用
        r = client.post("/api/agent-sessions/reset",
                        json={"platform": "wechat", "chat_id": "c1"})
        assert r.status_code == 200 and r.json()["reset"] is True
        assert "sess-2" in _simulate(client, "c1")  # 重置后开新会话


def test_reset_pi_session_changes_session_id(tmp_path):
    with _client(str(tmp_path / "p.db")) as client:
        _configure(client, tmp_path, FAKE_PI, "pi")
        sid1 = _simulate(client, "p1")
        assert sid1.startswith("chatagent-")
        client.post("/api/agent-sessions/reset", json={"platform": "wechat", "chat_id": "p1"})
        sid2 = _simulate(client, "p1")
        assert sid2.startswith("chatagent-") and sid2 != sid1


def test_reset_endpoint_validation(tmp_path):
    with _client(str(tmp_path / "v.db")) as client:
        assert client.post("/api/agent-sessions/reset",
                           json={"platform": "web", "chat_id": "x"}).status_code == 400
        assert client.post("/api/agent-sessions/reset",
                           json={"platform": "wechat", "chat_id": " "}).status_code == 400
        # Mock/LLM 引擎无会话概念 → reset=False 但不报错
        r = client.post("/api/agent-sessions/reset", json={"platform": "wechat", "chat_id": "any"})
        assert r.status_code == 200 and r.json()["reset"] is False
