from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
import zipfile

from fastapi.testclient import TestClient
from novelai_python._exceptions import APIError

from helpers import FakeImageHosting, FakeUpstream, write_test_config, write_test_config_with_upstreams
from app.queue_manager import QueueFull


class FailingAPIErrorUpstream(FakeUpstream):
    async def generate_image_payload_zip(self, payload):
        self.generate_started_at.append(0)
        self.last_generate_payload = payload
        raise APIError(
            "bad token",
            request=payload,
            response={"message": "bad token"},
            code="401",
        )


class EmptyZipUpstream(FakeUpstream):
    async def generate_image_payload_zip(self, payload):
        self.generate_started_at.append(0)
        self.last_generate_payload = payload
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w") as archive:
            archive.writestr("metadata.txt", b"no image")
        return buffer.getvalue()


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


def test_admin_upstream_test_uses_selected_upstream_and_fixed_payload(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"])))
    from app.main import app

    with TestClient(app) as client:
        upstream_a = FakeUpstream()
        upstream_b = FakeUpstream()
        image_hosting = FakeImageHosting()
        app.state.upstream = upstream_a
        app.state.upstream_clients["opus-b"] = upstream_b
        app.state.proxy_queue.image_hosting = image_hosting

        before_logs = app.state.db.query_one("SELECT COUNT(*) AS c FROM usage_logs")["c"]
        resp = client.post("/admin/api/upstreams/opus-b/test", auth=("admin", "admin123"))

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["upstream_id"] == "opus-b"
        assert body["zip_bytes"] > 0
        assert body["image_count"] == 1
        assert body["preview_image"]["filename"] == "image.png"
        assert body["preview_image"]["content_type"] == "image/png"
        assert body["preview_image"]["bytes"] == len(b"fake-image")
        assert body["preview_image"]["data_url"].startswith("data:image/png;base64,")
        assert len(upstream_a.generate_started_at) == 0
        assert len(upstream_b.generate_started_at) == 1
        payload = upstream_b.last_generate_payload
        assert payload["model"] == "nai-diffusion-4-5-full"
        assert payload["input"] == "A simple red apple on a white plate."
        assert payload["parameters"]["width"] == 512
        assert payload["parameters"]["height"] == 512
        assert payload["parameters"]["steps"] == 28
        assert payload["parameters"]["n_samples"] == 1
        assert app.state.db.query_one("SELECT COUNT(*) AS c FROM usage_logs")["c"] == before_logs
        assert image_hosting.uploaded_request_ids == []


def test_admin_upstream_test_returns_api_error_details(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a"])))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FailingAPIErrorUpstream()

        resp = client.post("/admin/api/upstreams/opus-a/test", auth=("admin", "admin123"))

        assert resp.status_code == 401
        body = resp.json()
        assert body["ok"] is False
        assert body["upstream_id"] == "opus-a"
        assert body["error_code"] == "401"
        assert body["error_type"] == "APIError"
        assert body["message"] == "bad token"
        assert isinstance(body["elapsed_ms"], int)


def test_admin_upstream_test_rejects_zip_without_images(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a"])))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = EmptyZipUpstream()

        resp = client.post("/admin/api/upstreams/opus-a/test", auth=("admin", "admin123"))

        assert resp.status_code == 502
        body = resp.json()
        assert body["ok"] is False
        assert body["error_code"] == "invalid_upstream_response"
        assert body["error_type"] == "InvalidUpstreamResponse"


def test_admin_upstream_test_unknown_upstream_returns_400(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a"])))
    from app.main import app

    with TestClient(app) as client:
        resp = client.post("/admin/api/upstreams/missing/test", auth=("admin", "admin123"))

        assert resp.status_code == 400
        body = resp.json()
        assert body["ok"] is False
        assert body["error_code"] == "unknown_upstream"


def test_admin_upstream_test_queue_full_returns_503(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a"])))
    from app.main import app

    class QueueFullProbeQueue:
        def qsize(self):
            return 0

        def get_weights(self):
            return {"strategy": "round_robin", "upstreams": [{"id": "opus-a", "score": 0.8, "weight": 0.95, "queue_size": 0, "running": False}]}

        def snapshot(self):
            return {"queue_size": 0, "running": None, "running_items": [], "queued": [], "dispatch_queue_size": 0, "upstreams": []}

        async def submit_upstream_probe(self, **_kwargs):
            raise QueueFull

    with TestClient(app) as client:
        app.state.proxy_queue = QueueFullProbeQueue()

        resp = client.post("/admin/api/upstreams/opus-a/test", auth=("admin", "admin123"))

        assert resp.status_code == 503
        body = resp.json()
        assert body["ok"] is False
        assert body["error_code"] == "queue_full"


def test_admin_dashboard_includes_upstream_test_modal_and_fetch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a"])))
    from app.main import app

    with TestClient(app) as client:
        client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        dashboard = client.get("/admin")

        assert dashboard.status_code == 200
        assert 'id="upstream-test-modal"' in dashboard.text
        assert "data-upstream-test" in dashboard.text
        assert "upstream-test-preview" in dashboard.text
        assert "/admin/api/upstreams/" in dashboard.text
        assert "encodeURIComponent(activeUpstreamTestId)" in dashboard.text
