"""REST API + WebSocket 测试。"""
from __future__ import annotations

from app.models import Platform


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_status(client):
    data = client.get("/api/status").json()
    assert data["engine"] == "mock"
    assert data["auto_send"] is False  # 默认人工确认
    assert data["send_mode"] == "manual_confirm"
    assert set(data["connectors"]) == {"wechat", "dingtalk"}
    # 安全默认：监听不自动开启，由前端按钮控制
    assert data["connectors"]["wechat"]["status"] == "stopped"
    assert data["connectors"]["dingtalk"]["status"] == "stopped"
    assert data["connectors"]["wechat"]["mode"] == "mock"


def test_config_masks_keys(client):
    data = client.get("/api/config").json()
    assert "llm" in data and "acp" in data


def test_simulate_wechat_generates_reply(client):
    r = client.post(
        "/api/simulate",
        json={"platform": "wechat", "chat_id": "wx1", "sender": "客户A", "content": "你好，在吗？"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["message"]["direction"] == "in"
    assert data["message"]["platform"] == "wechat"
    assert data["reply"] is not None
    assert data["reply"]["status"] == "pending"
    assert data["reply"]["reply_to"] == data["message"]["id"]


def test_simulate_dingtalk(client):
    r = client.post(
        "/api/simulate",
        json={"platform": "dingtalk", "chat_id": "dt1", "sender": "老板", "content": "辛苦了，谢谢！"},
    )
    assert r.status_code == 200
    assert r.json()["reply"]["content"]


def test_simulate_invalid_platform(client):
    r = client.post("/api/simulate", json={"platform": "qq", "content": "hi"})
    assert r.status_code == 422


def test_messages_and_replies_listing(client):
    client.post("/api/simulate", json={"platform": "wechat", "chat_id": "wx2", "content": "你好"})
    msgs = client.get("/api/messages", params={"chat_id": "wx2"}).json()["messages"]
    assert [m["direction"] for m in msgs] == ["in", "out"]
    replies = client.get("/api/replies", params={"status": "pending"}).json()["replies"]
    assert any(r["chat_id"] == "wx2" for r in replies)


def test_approve_flow(client):
    data = client.post(
        "/api/simulate", json={"platform": "wechat", "chat_id": "wx3", "content": "你好"}
    ).json()
    reply_id = data["reply"]["id"]
    # 不带 body：发送原稿
    r = client.post(f"/api/replies/{reply_id}/approve")
    assert r.status_code == 200
    assert r.json()["message"]["status"] == "sent"
    # 再次审批 → 400
    assert client.post(f"/api/replies/{reply_id}/approve").status_code == 400
    # 不存在的回复 → 404
    assert client.post("/api/replies/msg_not_exist/approve").status_code == 404


def test_approve_with_edited_content(client):
    """填入输入框模式：批准时可携带编辑后的草稿内容。"""
    data = client.post(
        "/api/simulate", json={"platform": "wechat", "chat_id": "wx4", "content": "你好"}
    ).json()
    reply_id = data["reply"]["id"]
    original = data["reply"]["content"]
    r = client.post(f"/api/replies/{reply_id}/approve", json={"content": "人工编辑后的回复"})
    assert r.status_code == 200
    sent = r.json()["message"]
    assert sent["content"] == "人工编辑后的回复"
    assert sent["meta"]["edited_from"] == original


def test_select_candidate(client):
    """多候选切换：mock 引擎返回 2 条候选，切换后草稿正文更新。"""
    data = client.post(
        "/api/simulate", json={"platform": "wechat", "chat_id": "wx5", "content": "你好"}
    ).json()
    reply = data["reply"]
    cands = reply["meta"]["candidates"]
    assert len(cands) >= 2
    original = reply["content"]

    r = client.post(f"/api/replies/{reply['id']}/select", json={"index": 1})
    assert r.status_code == 200
    m = r.json()["message"]
    assert m["content"] == cands[1]["content"]
    assert m["meta"]["selected_candidate"] == 1
    assert m["meta"]["edited_from"] == original

    # 切换后批准 → 发送的是选中候选
    r2 = client.post(f"/api/replies/{reply['id']}/approve")
    assert r2.json()["message"]["content"] == cands[1]["content"]

    # 越界 → 400；已发送的回复再切换 → 400
    assert client.post(f"/api/replies/{reply['id']}/select", json={"index": 9}).status_code == 400
    assert client.post(f"/api/replies/{reply['id']}/select", json={"index": 0}).status_code == 400


def test_reject_flow(client):
    data = client.post(
        "/api/simulate", json={"platform": "dingtalk", "chat_id": "dt3", "content": "你好"}
    ).json()
    reply_id = data["reply"]["id"]
    r = client.post(f"/api/replies/{reply_id}/reject")
    assert r.status_code == 200
    assert r.json()["message"]["status"] == "rejected"


def test_direct_chat_api(client):
    r = client.post("/api/chat", json={"content": "你好"})
    assert r.status_code == 200
    data = r.json()
    assert data["message"]["platform"] == "web"
    assert data["reply"]["direction"] == "out"
    assert client.post("/api/chat", json={"content": "  "}).status_code == 400


def test_connector_stop_start(client):
    r = client.post("/api/connectors/wechat/stop")
    assert r.status_code == 200
    assert r.json()["connector"]["status"] == "stopped"
    r = client.post("/api/connectors/wechat/start")
    assert r.status_code == 200
    assert r.json()["connector"]["status"] == "running"
    assert client.post("/api/connectors/qq/stop").status_code == 404


def test_websocket_hello_recovers_active_processing(client):
    client.app.state.settings.debounce_seconds = 30
    response = client.post('/api/simulate', json={
        'platform': 'wechat', 'chat_id': 'restore-progress', 'content': '等待处理',
    })
    assert response.status_code == 200
    incoming_id = response.json()['message']['id']
    with client.websocket_connect('/ws') as ws:
        hello = ws.receive_json()
        state = hello['processing'][incoming_id]
        assert state['phase'] == 'merging'
        assert state['chat_id'] == 'restore-progress'
        assert state['since'] and state['wait'] == 30


def test_websocket_events(client):
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        client.post(
            "/api/simulate", json={"platform": "wechat", "chat_id": "ws1", "content": "你好"}
        )
        assert hello["processing"] == {}
        events = []
        for _ in range(10):
            event = ws.receive_json()
            events.append(event)
            if event["type"] == "message_updated" and event["message"].get("meta", {}).get("processing", {}).get("finished_at"):
                break
        assert events[0]["type"] == "message"
        assert any(ev["type"] == "reply" for ev in events)
        phases = [ev["state"]["phase"] for ev in events if ev["type"] == "processing"]
        assert phases == ["preparing", "thinking", "completed"]
        assert events[-1]["message"]["meta"]["processing"]["phase"] == "completed"
