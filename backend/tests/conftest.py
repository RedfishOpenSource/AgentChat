"""测试公共夹具。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import ConnectorSettings, JEVSettings, LLMSettings, Settings
from app.main import create_app


def make_settings(**overrides) -> Settings:
    s = Settings(
        engine="mock",
        debounce_seconds=0.0,  # 默认关闭合并，保持既有测试的同步语义；debounce 专项测试显式开启
        db_path=":memory:",  # 测试默认不落盘；持久化测试显式传 tmp 文件
    log_file="",          # 测试不开文件日志（避免多进程句柄）
    config_file="",       # 测试不写配置文件（热更新 persist 测试显式传 tmp 文件）
        llm=LLMSettings(max_history=12),
        jev=JEVSettings(enabled=False),
        wechat=ConnectorSettings(enabled=True, mode="mock", poll_interval=0.02),
        dingtalk=ConnectorSettings(enabled=True, mode="mock", poll_interval=0.02),
    )
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


@pytest.fixture()
def settings():
    return make_settings()


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    # 显式以 127.0.0.1 访问（默认白名单仅允许本机）
    with TestClient(app, client=("127.0.0.1", 12345)) as c:
        yield c
