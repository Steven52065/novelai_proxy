from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helpers import write_test_config


def test_admin_dashboard_websocket_sends_snapshot_for_session(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        with client.websocket_connect("/admin/ws/dashboard", headers={"Origin": "http://testserver"}) as websocket:
            body = websocket.receive_json()

        assert body["type"] == "dashboard.snapshot"
        assert "stats" in body
        assert "queue" in body
        assert "upstream_weights" in body
        assert body["request_trends"] is None

def test_admin_dashboard_websocket_accepts_forwarded_origin(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        with client.websocket_connect(
            "/admin/ws/dashboard",
            headers={
                "Origin": "https://admin.example.com",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "admin.example.com",
            },
        ) as websocket:
            body = websocket.receive_json()

        assert body["type"] == "dashboard.snapshot"

def test_admin_dashboard_websocket_accepts_same_host_https_origin_without_forwarded_proto(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        with client.websocket_connect(
            "/admin/ws/dashboard",
            headers={
                "Host": "admin.example.com",
                "Origin": "https://admin.example.com",
            },
        ) as websocket:
            body = websocket.receive_json()

        assert body["type"] == "dashboard.snapshot"

def test_admin_dashboard_websocket_sends_heartbeat_when_idle(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    import app.admin.dashboard as dashboard_module
    from app.main import app

    monkeypatch.setattr(dashboard_module, "DASHBOARD_WS_HEARTBEAT_SECONDS", 0.05)

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        with client.websocket_connect("/admin/ws/dashboard", headers={"Origin": "http://testserver"}) as websocket:
            assert websocket.receive_json()["type"] == "dashboard.snapshot"
            heartbeat = websocket.receive_json()

        assert heartbeat["type"] == "dashboard.heartbeat"
        assert "stats" not in heartbeat
        assert "queue" not in heartbeat
        assert "upstream_weights" not in heartbeat

def test_admin_dashboard_websocket_sends_snapshot_after_data_change(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        with client.websocket_connect("/admin/ws/dashboard", headers={"Origin": "http://testserver"}) as websocket:
            first = websocket.receive_json()
            assert first["type"] == "dashboard.snapshot"
            assert first["stats"]["total_users"] == 0

            create_resp = client.post(
                "/admin/api/users",
                auth=("admin", "admin123"),
                json={"name": "ws-user", "tier": "normal", "anlas_total": 100},
            )
            assert create_resp.status_code == 200
            changed = websocket.receive_json()

        assert changed["type"] == "dashboard.snapshot"
        assert changed["stats"]["total_users"] == 1

def test_admin_dashboard_websocket_rejects_cross_origin(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/admin/ws/dashboard",
                headers={"Origin": "http://evil.example"},
            ):
                pass

        assert exc_info.value.code == 1008
