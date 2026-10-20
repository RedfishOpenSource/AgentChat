"""配置热更新测试：引擎热切、参数热生效、非法值拒绝、持久化写盘。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from tests.conftest import make_settings


@pytest.fixture()
def hot_client(tmp_path):
    settings = make_settings(config_file=str(tmp_path / "config.yaml"))
    with TestClient(create_app(settings), client=("127.0.0.1", 12345)) as c:
        yield c


def test_engine_hot_swap(hot_client):
    r = hot_client.put("/api/config", json={"engine": "llm"})
    assert r.status_code == 200
    assert "engine" in r.json()["changed"]
    assert r.json()["config"]["engine"] == "llm"
    assert hot_client.get("/api/status").json()["engine"] == "llm"

    r = hot_client.put("/api/config", json={"engine": "mock"})
    assert r.json()["config"]["engine"] == "mock"
    assert hot_client.get("/api/status").json()["engine"] == "mock"


def test_engine_invalid_rejected(hot_client):
    r = hot_client.put("/api/config", json={"engine": "chatgpt"})
    assert r.status_code == 400


def test_params_hot_apply(hot_client):
    r = hot_client.put(
        "/api/config",
        json={
            "num_candidates": 3,
            "debounce_seconds": 1.5,
            "group_trigger": "all",
            "non_text_action": "skip",
            "llm_rate_limit_per_chat": 5,
            "allowed_contacts": ["张三"],
        },
    )
    assert r.status_code == 200
    cfg = r.json()["config"]
    assert cfg["llm"]["num_candidates"] == 3
    assert cfg["debounce_seconds"] == 1.5
    assert cfg["group_trigger"] == "all"
    assert cfg["non_text_action"] == "skip"
    assert cfg["llm_rate_limit_per_chat"] == 5
    assert cfg["allowed_contacts"] == ["张三"]


def test_invalid_values_rejected(hot_client):
    assert hot_client.put("/api/config", json={"group_trigger": "everything"}).status_code == 400
    assert hot_client.put("/api/config", json={"num_candidates": 99}).status_code == 400
    assert hot_client.put("/api/config", json={"non_text_action": "yell"}).status_code == 400


def test_persist_writes_yaml(hot_client):
    r = hot_client.put(
        "/api/config",
        json={"engine": "llm", "group_trigger": "all", "debounce_seconds": 2.0, "persist": True},
    )
    assert r.status_code == 200
    assert r.json()["persisted"] is True

    # 写出的 YAML 能被 load_settings 读回
    cfg_file = hot_client.app.state.settings.config_file
    data = yaml.safe_load(Path(cfg_file).read_text(encoding="utf-8"))
    assert data["engine"] == "llm"
    assert data["group_trigger"] == "all"
    assert data["debounce_seconds"] == 2.0
    # load_settings 读回也一致
    s2 = load_settings(cfg_file)
    assert s2.engine == "llm" and s2.group_trigger == "all"


def test_nested_settings_and_auto_send_persist_roundtrip(hot_client):
    r = hot_client.put("/api/config", json={
        "llm_max_retries": 4,
        "llm_rate_limit_per_chat": 6, "auto_send": True, "persist": True,
    })
    assert r.status_code == 200
    loaded = load_settings(hot_client.app.state.settings.config_file)
    assert loaded.llm.max_retries == 4
    assert loaded.llm_rate_limit_per_chat == 6
    assert loaded.auto_send is True


def test_invalid_config_update_does_not_partially_mutate(hot_client):
    r = hot_client.put("/api/config", json={"engine": "llm", "group_trigger": "invalid"})
    assert r.status_code == 400
    assert hot_client.get("/api/config").json()["engine"] == "mock"


def test_llm_style_hot_apply_and_reject(hot_client):
    """llm_style 热更新：合法值立即生效，非法值 400。"""
    r = hot_client.put("/api/config", json={"llm_style": "formal"})
    assert r.status_code == 200
    assert "llm_style" in r.json()["changed"]
    assert hot_client.get("/api/config").json()["llm"]["style"] == "formal"
    assert hot_client.put("/api/config", json={"llm_style": "nope"}).status_code == 400


def test_llm_style_persist_writes_yaml(hot_client):
    r = hot_client.put("/api/config", json={"llm_style": "casual", "persist": True})
    assert r.json()["persisted"] is True
    cfg_file = hot_client.app.state.settings.config_file
    data = yaml.safe_load(Path(cfg_file).read_text(encoding="utf-8"))
    assert data["llm"]["style"] == "casual"
    assert load_settings(cfg_file).llm.style == "casual"


def test_panel_settings_persist_in_sqlite_across_restart(tmp_path):
    db_path = str(tmp_path / "settings.db")
    first = TestClient(create_app(make_settings(db_path=db_path)), client=("127.0.0.1", 12345))
    with first as client:
        response = client.put("/api/config", json={
            "engine": "acp",
            "acp_mode": "acp",
            "acp_command": "pi-acp",
            "acp_args": "--quiet",
            "acp_cwd": str(tmp_path),
            "acp_timeout": 240,
            "group_trigger": "all",
            "non_text_action": "skip",
            "debounce_seconds": 2.5,
            "acp_env_name": "TEST_AGENT_KEY",
            "acp_env_key": "test-only-credential",
            "persist_to_db": True,
        })
        assert response.status_code == 200
        assert response.json()["persisted"] is True
        assert first.app.state.db.get_settings()["acp_env_key"] == "test-only-credential"
        # 部分更新不应删除先前保存的工作空间和 Agent。
        assert client.put("/api/config", json={
            "debounce_seconds": 0, "persist_to_db": True,
        }).status_code == 200

    second = TestClient(create_app(make_settings(db_path=db_path)), client=("127.0.0.1", 12345))
    with second as client:
        config = client.get("/api/config").json()
        assert config["engine"] == "acp"
        assert config["acp"]["command"] == "pi-acp"
        assert config["acp"]["args"] == "--quiet"
        assert config["acp"]["cwd"] == str(tmp_path)
        assert config["acp"]["timeout"] == 240
        assert config["group_trigger"] == "all"
        assert config["non_text_action"] == "skip"
        assert config["debounce_seconds"] == 0
        assert config["acp"]["env_name"] == "TEST_AGENT_KEY"
        assert config["acp"]["env_key"] == "test-only-credential"
        assert config["auto_send"] is False


def test_persist_requires_config_file():
    settings = make_settings(config_file="")  # 测试默认无配置文件路径
    with TestClient(create_app(settings), client=("127.0.0.1", 12345)) as client:
        r = client.put("/api/config", json={"engine": "llm", "persist": True})
        assert r.status_code == 400
        # 不带 persist 的热更新仍然有效
        assert client.put("/api/config", json={"engine": "llm"}).status_code == 200


def test_mock_engine_meta_has_candidates_after_hot_config(client):
    """热更新后 mock 引擎仍正常工作（冒烟）。"""
    r = client.put("/api/config", json={"debounce_seconds": 0})
    assert r.status_code == 200
    data = client.post("/api/simulate", json={"platform": "wechat", "chat_id": "hot1", "content": "你好"}).json()
    assert data["reply"]["meta"]["candidates"]
