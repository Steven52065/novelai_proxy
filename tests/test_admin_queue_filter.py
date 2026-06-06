from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from helpers import write_test_config_with_upstreams


def test_admin_queue_status_can_filter_by_upstream(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"])))
    from app.main import app

    class MultiUpstreamQueueSnapshot:
        def qsize(self):
            return 2

        def snapshot(self):
            return {
                "queue_size": 2,
                "running": None,
                "running_items": [
                    {
                        "request_id": "running-a",
                        "user_id": 1,
                        "action": "generate",
                        "tier": "normal",
                        "upstream_id": "opus-a",
                        "estimated_anlas_cost": 0,
                        "priority": 10,
                        "position": 0,
                        "status": "running",
                        "queued_seconds": 3,
                    },
                    {
                        "request_id": "running-b",
                        "user_id": 1,
                        "action": "generate",
                        "tier": "normal",
                        "upstream_id": "opus-b",
                        "estimated_anlas_cost": 0,
                        "priority": 10,
                        "position": 0,
                        "status": "running",
                        "queued_seconds": 3,
                    },
                ],
                "queued": [
                    {
                        "request_id": "queued-a",
                        "user_id": 1,
                        "action": "generate",
                        "tier": "normal",
                        "upstream_id": "opus-a",
                        "estimated_anlas_cost": 0,
                        "priority": 10,
                        "position": 1,
                        "status": "queued",
                        "queued_seconds": 8,
                    },
                    {
                        "request_id": "queued-b",
                        "user_id": 1,
                        "action": "generate",
                        "tier": "normal",
                        "upstream_id": "opus-b",
                        "estimated_anlas_cost": 0,
                        "priority": 10,
                        "position": 2,
                        "status": "queued",
                        "queued_seconds": 8,
                    },
                ],
            }

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "queue-upstream-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        now = datetime.now(timezone.utc).isoformat()
        for request_id in ("running-a", "running-b", "queued-a", "queued-b"):
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, user_id, "generate", 0, "queued", "INFO", now),
            )
        app.state.proxy_queue = MultiUpstreamQueueSnapshot()

        resp = client.get("/admin/api/queue?upstream_id=opus-b", auth=("admin", "admin123"))

        assert resp.status_code == 200
        body = resp.json()
        assert body["queue_size"] == 1
        assert [item["request_id"] for item in body["running_items"]] == ["running-b"]
        assert body["running"]["request_id"] == "running-b"
        assert [item["request_id"] for item in body["queued"]] == ["queued-b"]
        assert body["queued"][0]["position"] == 1
