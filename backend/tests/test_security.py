"""IP 白名单访问控制测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.security import ip_allowed, normalize_ip
from tests.conftest import make_settings


def test_normalize_and_match():
    assert normalize_ip("::ffff:127.0.0.1") == "127.0.0.1"
    assert ip_allowed("::ffff:127.0.0.1", ["127.0.0.1"]) is True
    assert ip_allowed("192.168.1.5", ["*"]) is True
    assert ip_allowed("192.168.1.5", ["127.0.0.1", "::1"]) is False
    assert ip_allowed("", ["127.0.0.1"]) is False


def test_default_blocks_foreign_ip():
    """默认白名单（仅本机）：局域网 IP 应被 403 拒绝。"""
    app = create_app(make_settings())
    foreign = TestClient(app, client=("192.168.1.50", 5555))
    r = foreign.get("/api/health")
    assert r.status_code == 403
    assert "白名单" in r.json()["detail"]
    r = foreign.get("/api/messages")
    assert r.status_code == 403


def test_wildcard_allows_all():
    app = create_app(make_settings(allowed_ips=["*"]))
    foreign = TestClient(app, client=("10.20.30.40", 5555))
    assert foreign.get("/api/health").status_code == 200


def test_configured_ip_allowed():
    app = create_app(make_settings(allowed_ips=["192.168.1.50"]))
    allowed = TestClient(app, client=("192.168.1.50", 5555))
    assert allowed.get("/api/health").status_code == 200
    # 未在白名单的其他来源仍被拒
    other = TestClient(app, client=("10.0.0.9", 5555))
    assert other.get("/api/health").status_code == 403


def test_websocket_blocked_for_foreign_ip():
    app = create_app(make_settings())
    foreign = TestClient(app, client=("192.168.1.60", 5555))
    with pytest.raises(WebSocketDisconnect):
        with foreign.websocket_connect("/ws") as ws:
            ws.receive_json()


def test_websocket_allowed_for_local(settings):
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "hello"
