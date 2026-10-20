"""设置面板 SQLite 持久化：仅用临时数据库和 Mock 连接器。"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import apply_persisted_settings
from app.main import create_app
from tests.conftest import make_settings
from tests.test_config_hot import hot_client


@pytest.mark.parametrize("payload", [
    {"group_trigger": "invalid"},
    {"engine": "acp", "acp_command": ""},
    {"acp_timeout": 1},
    {"auto_send": True},
    {"persist": True, "group_trigger": "all"},
])
def test_invalid_sqlite_update_is_not_saved(hot_client, payload):
    before = hot_client.get("/api/config").json()
    response = hot_client.put("/api/config", json={**payload, "persist_to_db": True})
    assert response.status_code == 400
    assert hot_client.app.state.db.get_settings() == {}
    assert hot_client.get("/api/config").json() == before


def test_sqlite_failure_restores_runtime_config(hot_client, monkeypatch):
    before = hot_client.get("/api/config").json()

    def fail(_settings):
        raise sqlite3.OperationalError("test database write failure")

    monkeypatch.setattr(hot_client.app.state.db, "save_settings", fail)
    response = hot_client.put("/api/config", json={
        "group_trigger": "all", "debounce_seconds": 7, "persist_to_db": True,
    })
    assert response.status_code == 500
    assert hot_client.get("/api/config").json() == before
    assert hot_client.app.state.db.get_settings() == {}


def test_environment_overrides_sqlite_on_restart(tmp_path, monkeypatch):
    db_path = str(tmp_path / "config.db")
    with TestClient(create_app(make_settings(db_path=db_path)), client=("127.0.0.1", 12345)) as client:
        assert client.put("/api/config", json={
            "group_trigger": "all", "non_text_action": "skip",
            "debounce_seconds": 5, "persist_to_db": True,
        }).status_code == 200
    monkeypatch.setenv("CHATAGENT_GROUP_TRIGGER", "listen-only")
    monkeypatch.setenv("CHATAGENT_NON_TEXT_ACTION", "placeholder")
    monkeypatch.setenv("CHATAGENT_DEBOUNCE_SECONDS", "1.5")
    with TestClient(create_app(make_settings(db_path=db_path)), client=("127.0.0.1", 12345)) as client:
        config = client.get("/api/config").json()
        assert config["group_trigger"] == "listen-only"
        assert config["non_text_action"] == "placeholder"
        assert config["debounce_seconds"] == 1.5


def test_db_restore_ignores_fields_outside_panel():
    settings = apply_persisted_settings(make_settings(), {
        "auto_send": True, "db_path": "unexpected.db", "port": 9999,
        "group_trigger": "all",
    })
    assert settings.auto_send is False
    assert settings.db_path == ":memory:"
    assert settings.port == 8318
    assert settings.group_trigger == "all"
