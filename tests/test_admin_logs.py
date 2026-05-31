from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from helpers import PAYLOAD, FakeUpstream, write_test_config


def test_admin_can_replay_rejected_generate_without_quota_charge(tmp_path: Path, monkeypatch):
    config_path = write_test_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    paid_payload = PAYLOAD | {
        "parameters": PAYLOAD["parameters"] | {"width": 1024, "height": 1024, "steps": 50}
    }
    with TestClient(app) as client:
        fake_upstream = FakeUpstream()
        app.state.upstream = fake_upstream
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "replay-low-quota", "tier": "normal", "anlas_total": 1},
        )
        user_id = create_resp.json()["user_id"]
        api_key = create_resp.json()["api_key"]

        rejected = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=paid_payload)
        assert rejected.status_code == 402
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        rejected_log = next(row for row in logs if row["status"] == "rejected")

        replay = client.post(f"/admin/api/logs/{rejected_log['request_id']}/replay", auth=("admin", "admin123"))

        assert replay.status_code == 200
        body = replay.json()
        assert body["source_request_id"] == rejected_log["request_id"]
        assert body["replay_request_id"] != rejected_log["request_id"]
        assert body["images"][0]["filename"] == "image.png"
        assert body["images"][0]["data_url"].startswith("data:image/png;base64,")
        assert fake_upstream.last_post_binary_url == "https://image.novelai.net/ai/generate-image"
        assert fake_upstream.last_post_binary_payload == paid_payload

        quota = app.state.quota_manager.get_snapshot(user_id)
        assert quota.used == 0
        assert quota.reserved == 0

        replay_log = app.state.db.query_one(
            "SELECT action, status, estimated_anlas_cost, final_anlas_cost FROM usage_logs WHERE request_id = ?",
            (body["replay_request_id"],),
        )
        assert replay_log["action"] == "replay:generate"
        assert replay_log["status"] == "success"
        assert replay_log["estimated_anlas_cost"] == 0
        assert replay_log["final_anlas_cost"] == 0

def test_admin_logs_display_created_at_in_utc_plus_8(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "log-time-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        app.state.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, log_level, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("display-time-request", user_id, "generate", "nai-diffusion-3", 512, 768, 1, 1, 0, "success", "INFO", "2026-05-27T00:00:00+00:00"),
        )

        api_body = client.get("/admin/api/logs", auth=("admin", "admin123")).json()
        log = next(row for row in api_body["logs"] if row["request_id"] == "display-time-request")
        assert log["created_at"] == "2026-05-27T00:00:00+00:00"
        assert log["created_at_display"] == "2026-05-27 08:00:00 UTC+8"

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        logs_page = client.get("/admin/logs")
        assert logs_page.status_code == 200
        assert "2026-05-27 08:00:00 UTC+8" in logs_page.text

def test_admin_logs_filter_accepts_empty_user_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        logs_page = client.get("/admin/logs?user_id=&limit=100")
        assert logs_page.status_code == 200
        assert "使用日志审计" in logs_page.text

def test_admin_logs_api_supports_session_pagination(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "log-pagination-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        for index in range(5):
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (f"page-log-{index}", user_id, "generate", 0, "success", "INFO", f"2026-05-27T00:00:0{index}+00:00"),
            )

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        first_page = client.get("/admin/api/logs?limit=2")
        assert first_page.status_code == 200
        first_body = first_page.json()
        assert [row["request_id"] for row in first_body["logs"]] == ["page-log-4", "page-log-3"]
        assert first_body["has_more"] is True
        first_cursor = first_body["next_before_id"]
        assert first_cursor == first_body["logs"][-1]["id"]

        # A new log arriving between page fetches must not shift the cursor window.
        app.state.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("page-log-newer", user_id, "generate", 0, "success", "INFO", "2026-05-27T00:00:09+00:00"),
        )

        second_page = client.get(f"/admin/api/logs?limit=2&before_id={first_cursor}")
        assert second_page.status_code == 200
        second_body = second_page.json()
        assert [row["request_id"] for row in second_body["logs"]] == ["page-log-2", "page-log-1"]
        assert second_body["has_more"] is True
        second_cursor = second_body["next_before_id"]
        assert second_cursor == second_body["logs"][-1]["id"]

        final_page = client.get(f"/admin/api/logs?limit=2&before_id={second_cursor}")
        assert final_page.status_code == 200
        final_body = final_page.json()
        assert [row["request_id"] for row in final_body["logs"]] == ["page-log-0"]
        assert final_body["has_more"] is False
        assert final_body["next_before_id"] == final_body["logs"][-1]["id"]

        logs_page = client.get("/admin/logs?limit=2")
        assert logs_page.status_code == 200
        newest_page = client.get("/admin/api/logs?limit=2")
        newest_body = newest_page.json()
        assert f'data-next-before-id="{newest_body["next_before_id"]}"' in logs_page.text
        assert 'data-has-more="true"' in logs_page.text
        assert "window.localStorage" in logs_page.text
        assert "readStoredValue" in logs_page.text
        assert "IntersectionObserver" in logs_page.text
