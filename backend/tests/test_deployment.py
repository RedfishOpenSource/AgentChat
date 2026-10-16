"""部署相关测试：前端静态托管、日志文件。"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings


def test_serves_frontend_dist(tmp_path):
    """配置 static_dir 后，/ 返回前端页面，/api 仍为 JSON。"""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>frontend-ok</body></html>", encoding="utf-8")

    with TestClient(create_app(make_settings(static_dir=str(dist))), client=("127.0.0.1", 12345)) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "frontend-ok" in r.text
        r = c.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_missing_dist_serves_api_only():
    """dist 不存在时不挂载静态目录：/ 返回 404 JSON，API 正常。"""
    with TestClient(create_app(make_settings(static_dir=str(Path("Z:/nonexistent")))),
                    client=("127.0.0.1", 12345)) as c:
        assert c.get("/").status_code == 404
        assert c.get("/api/health").status_code == 200


def test_log_file_created(tmp_path):
    log_file = tmp_path / "logs" / "app.log"
    with TestClient(create_app(make_settings(log_file=str(log_file))),
                    client=("127.0.0.1", 12345)) as c:
        c.get("/api/health")
    assert log_file.exists()
    assert log_file.read_text(encoding="utf-8", errors="ignore").strip()
