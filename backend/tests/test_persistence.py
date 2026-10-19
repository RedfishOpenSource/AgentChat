"""SQLite 持久化测试：落盘、恢复（重启场景）、审计日志。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings


def test_sqlite_roundtrip(tmp_path):
    from app.models import ChatMessage, Platform
    from app.persistence import SqliteStore

    db = SqliteStore(str(tmp_path / "t.db"))
    m = ChatMessage(platform=Platform.WECHAT, chat_id="c1", sender="张三", content="你好")
    db.upsert_message(m)
    m.status = "pending"
    m.meta = {"k": "v"}
    db.upsert_message(m)  # 更新同一条

    loaded = db.load_recent(10)
    assert len(loaded) == 1
    assert loaded[0].id == m.id
    assert loaded[0].status == "pending"
    assert loaded[0].meta == {"k": "v"}
    assert db.count_messages() == 1
    db.close()


def test_message_scenario_fields_survive_restart_and_old_drafts_are_retrievable(tmp_path):
    from app.models import ChatMessage, Platform, STATUS_PENDING
    from app.persistence import SqliteStore
    from app.store import MessageStore

    db = SqliteStore(str(tmp_path / "fields.db"))
    store = MessageStore(max_size=1, db=db)
    old = ChatMessage(platform=Platform.WECHAT, chat_id="group", sender="A", content="@me",
                      msg_type="image", is_group=True, at_me=True, status=STATUS_PENDING)
    store.add(old)
    store.add(ChatMessage(platform=Platform.WECHAT, chat_id="other", sender="B", content="new"))
    assert store.get(old.id).is_group is True
    loaded = db.get_message(old.id)
    assert loaded.msg_type == "image" and loaded.is_group is True and loaded.at_me is True
    db.close()


def test_restart_restores_messages_and_drafts(tmp_path):
    """重启场景：关闭应用后重新创建，历史消息与待确认草稿应被恢复。"""
    db_file = str(tmp_path / "chatagent.db")

    # ---- 第一次运行 ----
    settings = make_settings(db_path=db_file)
    with TestClient(create_app(settings), client=("127.0.0.1", 12345)) as client:
        data = client.post(
            "/api/simulate", json={"platform": "wechat", "chat_id": "wx1", "content": "你好"}
        ).json()
        assert data["reply"]["status"] == "pending"

    # ---- 第二次运行（模拟重启） ----
    with TestClient(create_app(make_settings(db_path=db_file)), client=("127.0.0.1", 12345)) as client:
        msgs = client.get("/api/messages", params={"chat_id": "wx1"}).json()["messages"]
        assert [m["direction"] for m in msgs] == ["in", "out"]
        assert msgs[0]["content"] == "你好"
        # 未确认草稿恢复后仍可审批
        pending = client.get("/api/replies", params={"status": "pending"}).json()["replies"]
        assert any(r["id"] == data["reply"]["id"] for r in pending)
        r = client.post(f"/api/replies/{data['reply']['id']}/approve")
        assert r.status_code == 200
        assert r.json()["message"]["status"] == "sent"


def test_audit_log_written(tmp_path):
    db_file = str(tmp_path / "a.db")
    with TestClient(create_app(make_settings(db_path=db_file)), client=("127.0.0.1", 12345)) as client:
        data = client.post(
            "/api/simulate", json={"platform": "wechat", "chat_id": "wx1", "content": "你好"}
        ).json()
        reply_id = data["reply"]["id"]
        client.post(f"/api/replies/{reply_id}/approve", json={"content": "编辑后的回复"})
        client.post(f"/api/replies/{reply_id}/reject")  # 应失败（已发送），不产生审计

        audit = client.get("/api/audit").json()["audit"]
        assert len(audit) == 1
        row = audit[0]
        assert row["action"] == "approve"
        assert row["actor"] == "admin"
        assert "编辑后的回复" in row["detail"]
        assert row["message_id"] == reply_id


def test_memory_db_still_works(client):
    """:memory: 模式（测试默认）不受影响。"""
    r = client.post("/api/simulate", json={"platform": "wechat", "chat_id": "m1", "content": "hi"})
    assert r.status_code == 200
    assert client.get("/api/audit").json()["audit"] == []
