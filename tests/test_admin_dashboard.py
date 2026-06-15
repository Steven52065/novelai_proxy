from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from helpers import write_test_config


def test_admin_login_page(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"}, follow_redirects=False)
        assert login.status_code == 303
        assert "novelai_proxy_admin" in login.headers["set-cookie"]
        assert "Max-Age=2592000" in login.headers["set-cookie"]

        dashboard = client.get("/admin")
        assert dashboard.status_code == 200
        assert "novelai_proxy_admin" in dashboard.headers["set-cookie"]
        assert "Max-Age=2592000" in dashboard.headers["set-cookie"]
        assert "仪表盘" in dashboard.text

def test_admin_invalid_session_cookie_is_not_refreshed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        client.cookies.set("novelai_proxy_admin", "invalid")
        dashboard = client.get("/admin", follow_redirects=False)

        assert dashboard.status_code == 303
        assert "set-cookie" not in dashboard.headers

def test_admin_dashboard_snapshot_api_combines_ui_data(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "snapshot-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        app.state.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, user_id, action, estimated_anlas_cost, final_anlas_cost, status, log_level, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("snapshot-success", user_id, "generate", 5, 5, "success", "INFO", datetime.now(timezone.utc).isoformat()),
        )

        resp = client.get("/admin/api/dashboard", auth=("admin", "admin123"))

        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "dashboard.snapshot"
        assert body["stats"]["total_users"] == 1
        assert body["stats"]["today_requests"] == 1
        assert body["stats"]["total_anlas"] == 5
        assert "queue" in body
        assert "upstream_weights" in body
        assert body["request_trends"] is None


def test_admin_dashboard_snapshot_limits_queue_rows(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.admin.dashboard import DASHBOARD_QUEUE_DISPLAY_LIMIT
    from app.main import app

    total_queued = DASHBOARD_QUEUE_DISPLAY_LIMIT + 5

    class LargeFakeQueue:
        def qsize(self):
            return total_queued

        def get_weights(self):
            return {"strategy": "round_robin", "upstreams": []}

        def snapshot(self):
            queued = [
                {
                    "request_id": f"queued-{index}",
                    "user_id": 1,
                    "action": "generate",
                    "tier": "normal",
                    "estimated_anlas_cost": 0,
                    "priority": 10,
                    "position": index,
                    "status": "queued",
                    "queued_seconds": index,
                }
                for index in range(1, total_queued + 1)
            ]
            return {
                "queue_size": total_queued,
                "running": None,
                "running_items": [],
                "queued": queued,
                "dispatch_queue_size": 0,
                "upstreams": [
                    {
                        "id": "default",
                        "queue_size": total_queued,
                        "running": None,
                        "queued": queued,
                    }
                ],
            }

    with TestClient(app) as client:
        app.state.proxy_queue = LargeFakeQueue()

        dashboard_resp = client.get("/admin/api/dashboard", auth=("admin", "admin123"))
        assert dashboard_resp.status_code == 200
        dashboard_queue = dashboard_resp.json()["queue"]
        assert len(dashboard_queue["queued"]) == DASHBOARD_QUEUE_DISPLAY_LIMIT
        assert dashboard_queue["queued_total"] == total_queued
        assert dashboard_queue["queued_hidden"] == 5
        assert dashboard_queue["queued_display_limit"] == DASHBOARD_QUEUE_DISPLAY_LIMIT
        assert dashboard_queue["upstreams"][0]["queued"] == []
        assert dashboard_queue["upstreams"][0]["queued_hidden"] == total_queued

        full_resp = client.get("/admin/api/queue", auth=("admin", "admin123"))
        assert full_resp.status_code == 200
        full_queue = full_resp.json()
        assert len(full_queue["queued"]) == total_queued
        assert "queued_hidden" not in full_queue
