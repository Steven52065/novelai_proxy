from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from helpers import write_test_config


def test_admin_database_management_clears_large_payloads(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    old_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    recent_time = datetime.now(timezone.utc).isoformat()
    large_payload = '{"image":"' + ("a" * 2048) + '"}'

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "db-clean-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        for request_id, created_at in (("old-large-payload", old_time), ("recent-large-payload", recent_time)):
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level,
                    request_payload, output_files, image_urls, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    user_id,
                    "generate",
                    0,
                    "success",
                    "INFO",
                    large_payload,
                    '["image.png"]',
                    '[{"url":"https://files.catbox.moe/image.png"}]',
                    created_at,
                ),
            )
        app.state.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, user_id, action, estimated_anlas_cost, status, log_level,
                request_payload, output_files, image_urls, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old-large-image-urls",
                user_id,
                "generate",
                0,
                "success",
                "INFO",
                None,
                "[]",
                '[{"url":"https://files.catbox.moe/' + ("b" * 2048) + '.png"}]',
                old_time,
            ),
        )

        stats = client.get("/admin/api/database/stats", auth=("admin", "admin123"))
        assert stats.status_code == 200
        assert stats.json()["usage_logs"]["logs_with_payload"] == 2

        clear_resp = client.post(
            "/admin/api/database/clear-payloads",
            auth=("admin", "admin123"),
            json={"older_than_days": 7, "min_payload_kb": 1, "clear_output_files": False},
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json()["updated_logs"] == 1

        old_row = app.state.db.query_one(
            "SELECT request_payload, output_files, image_urls FROM usage_logs WHERE request_id = ?",
            ("old-large-payload",),
        )
        recent_row = app.state.db.query_one(
            "SELECT request_payload, output_files, image_urls FROM usage_logs WHERE request_id = ?",
            ("recent-large-payload",),
        )
        assert old_row["request_payload"] is None
        assert old_row["output_files"] == '["image.png"]'
        assert old_row["image_urls"] == '[{"url":"https://files.catbox.moe/image.png"}]'
        assert recent_row["request_payload"] == large_payload

        clear_urls_resp = client.post(
            "/admin/api/database/clear-payloads",
            auth=("admin", "admin123"),
            json={"older_than_days": 7, "min_payload_kb": 1, "clear_image_urls": True},
        )
        assert clear_urls_resp.status_code == 200
        assert clear_urls_resp.json()["updated_logs"] == 1

        image_url_row = app.state.db.query_one(
            "SELECT request_payload, output_files, image_urls FROM usage_logs WHERE request_id = ?",
            ("old-large-image-urls",),
        )
        assert image_url_row["request_payload"] is None
        assert image_url_row["output_files"] == "[]"
        assert image_url_row["image_urls"] is None

def test_admin_database_management_deletes_old_logs_by_status(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    old_time = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    recent_time = datetime.now(timezone.utc).isoformat()

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "db-delete-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        rows = [
            ("old-success-log", "success", old_time),
            ("old-rejected-log", "rejected", old_time),
            ("recent-rejected-log", "rejected", recent_time),
        ]
        for request_id, status, created_at in rows:
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, user_id, "generate", 0, status, "INFO", created_at),
            )

        cleanup_resp = client.post(
            "/admin/api/database/cleanup-logs",
            auth=("admin", "admin123"),
            json={"older_than_days": 30, "statuses": ["rejected"]},
        )
        assert cleanup_resp.status_code == 200
        assert cleanup_resp.json()["deleted_logs"] == 1

        remaining_ids = {
            row["request_id"]
            for row in app.state.db.query_all("SELECT request_id FROM usage_logs ORDER BY request_id")
        }
        assert "old-rejected-log" not in remaining_ids
        assert {"old-success-log", "recent-rejected-log"} <= remaining_ids

def test_admin_database_page_and_vacuum(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        page = client.get("/admin/database")
        assert page.status_code == 200
        assert "数据库管理" in page.text
        assert "清空大 Payload" in page.text

        api_resp = client.post("/admin/api/database/vacuum", auth=("admin", "admin123"))
        assert api_resp.status_code == 200
        assert api_resp.json()["ok"] is True

        form_resp = client.post("/admin/database/vacuum", follow_redirects=False)
        assert form_resp.status_code == 303
        assert "/admin/database" in form_resp.headers["location"]
