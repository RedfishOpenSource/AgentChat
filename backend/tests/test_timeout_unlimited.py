"""超时=0 表示不限制：配置校验、联系人配置生效与生成链路不被打断。"""
from __future__ import annotations

import sys
import time

import pytest

from tests.test_config_hot import hot_client

FAKE_ACP_SLOW = r'''
import json, sys, time
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
        time.sleep(2.0)  # 慢于旧的 5 秒下限不可能测；只需超过普通单测节奏，配合 timeout=0 必须完整等到回复
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid,
            "update": {"sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": f"[{sid}] 慢分析完成"}}}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}}), flush=True)
'''


def test_timeout_zero_configs_accepted_and_invalid_rejected(hot_client):
    # 热更新配置：0 允许，3 拒绝
    assert hot_client.put("/api/config", json={"acp_timeout": 0}).status_code == 200
    r = hot_client.put("/api/config", json={"acp_timeout": 3})
    assert r.status_code == 400 and "不限制" in r.json()["detail"]
    # 联系人配置：0 允许，3 拒绝
    r = hot_client.put("/api/contact-configs", json={
        "platform": "wechat", "chat_id": "c1", "timeout": 0,
        "group_trigger": "at-mention", "non_text_action": "placeholder", "debounce_seconds": 0})
    assert r.status_code == 200 and r.json()["timeout"] == 0
    r = hot_client.put("/api/contact-configs", json={
        "platform": "wechat", "chat_id": "c1", "timeout": 3,
        "group_trigger": "at-mention", "non_text_action": "placeholder", "debounce_seconds": 0})
    assert r.status_code == 400 and "不限制" in r.json()["detail"]
    agent = hot_client.app.state.agent
    from app.models import Platform
    assert agent.contact_config_for(Platform.WECHAT, "c1")["timeout"] == 0.0


def test_timeout_zero_waits_for_slow_agent(hot_client, tmp_path):
    """联系人超时=0：慢于常规阈值的分析必须完整等到回复，不被中断。"""
    import app.api.routes as routes

    async def _fake_rewrite(settings, description):
        return f"优化后的角色：{description}"

    routes._llm_role_prompt = _fake_rewrite
    script = tmp_path / "fake_slow_acp.py"
    script.write_text(FAKE_ACP_SLOW, encoding="utf-8")
    assert hot_client.put("/api/config", json={
        "engine": "acp", "acp_mode": "acp", "acp_command": sys.executable,
        "acp_args": str(script), "acp_timeout": 0,
    }).status_code == 200
    assert hot_client.put("/api/contact-configs", json={
        "platform": "wechat", "chat_id": "slow", "timeout": 0,
        "group_trigger": "at-mention", "non_text_action": "placeholder", "debounce_seconds": 0,
    }).status_code == 200
    started = time.monotonic()
    r = hot_client.post("/api/simulate", json={"platform": "wechat", "chat_id": "slow", "content": "慢慢分析"})
    elapsed = time.monotonic() - started
    assert r.status_code == 200, r.text
    assert "慢分析完成" in r.json()["reply"]["content"]
    assert elapsed >= 2.0, f"生成被提前中断（仅 {elapsed:.2f}s）"
