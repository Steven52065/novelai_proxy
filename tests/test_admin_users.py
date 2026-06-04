from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import utc_now_iso
from helpers import BlockingFakeUpstream, PAYLOAD, write_test_config, write_test_config_with_upstreams


def test_admin_create_update_free_small_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "limited", "tier": "normal", "anlas_total": 100, "free_small_only": True},
        )
        assert create_resp.status_code == 200
        user_id = create_resp.json()["user_id"]

        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        created = next(row for row in users if row["id"] == user_id)
        assert created["free_small_only"] == 1
        assert created["allowed_endpoints"] == "generate-image"
        assert created["allowed_endpoints_list"] == ["generate-image"]

        update_resp = client.patch(
            f"/admin/api/users/{user_id}",
            auth=("admin", "admin123"),
            json={"free_small_only": False, "allowed_endpoints": ["generate-image", "upscale"]},
        )
        assert update_resp.status_code == 200
        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        updated = next(row for row in users if row["id"] == user_id)
        assert updated["free_small_only"] == 0
        assert updated["allowed_endpoints"] == "generate-image,upscale"
        assert updated["allowed_endpoints_list"] == ["generate-image", "upscale"]

def test_admin_returns_key_once_without_persisting_plaintext(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "key-user", "tier": "normal", "anlas_total": 100},
        )
        assert create_resp.status_code == 200
        user_id = create_resp.json()["user_id"]
        old_key = create_resp.json()["api_key"]

        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        created = next(row for row in users if row["id"] == user_id)
        assert created["api_key"] is None
        db_row = client.app.state.db.query_one("SELECT api_key FROM users WHERE id = ?", (user_id,))
        assert db_row["api_key"] is None

        reset_resp = client.post(f"/admin/api/users/{user_id}/reset-key", auth=("admin", "admin123"))
        assert reset_resp.status_code == 200
        new_key = reset_resp.json()["api_key"]
        assert new_key.startswith("nai_proxy_")
        assert new_key != old_key

        old_sub = client.get("/user/subscription", headers={"Authorization": f"Bearer {old_key}"})
        assert old_sub.status_code == 401

        new_sub = client.get("/user/subscription", headers={"Authorization": f"Bearer {new_key}"})
        assert new_sub.status_code == 200

        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        updated = next(row for row in users if row["id"] == user_id)
        assert updated["api_key"] is None
        db_row = client.app.state.db.query_one("SELECT api_key FROM users WHERE id = ?", (user_id,))
        assert db_row["api_key"] is None


def test_admin_web_key_flash_does_not_put_key_in_url_or_logs(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        create_resp = client.post(
            "/admin/users",
            data={
                "name": "web-key-user",
                "tier": "normal",
                "anlas_total": "100",
                "reset_period": "month",
                "reset_day": "1",
                "allowed_endpoints": "generate-image",
            },
            follow_redirects=False,
        )
        assert create_resp.status_code == 303
        assert create_resp.headers["location"] == "/admin/users"

        page = client.get("/admin/users")
        assert page.status_code == 200
        assert "nai_proxy_" in page.text

        second_page = client.get("/admin/users")
        assert "nai_proxy_" not in second_page.text

    log_text = (tmp_path / "logs" / "novelai_proxy.log").read_text(encoding="utf-8")
    assert "nai_proxy_" not in log_text


def test_admin_rejects_unknown_allowed_upstream(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a"])))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "bad-upstream", "tier": "normal", "anlas_total": 100, "allowed_upstreams": ["missing"]},
        )
        assert create_resp.status_code == 400
        assert create_resp.json()["message"] == "Unknown upstream id: missing"

        ok_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "good-upstream", "tier": "normal", "anlas_total": 100, "allowed_upstreams": ["opus-a"]},
        )
        user_id = ok_resp.json()["user_id"]
        update_resp = client.patch(
            f"/admin/api/users/{user_id}",
            auth=("admin", "admin123"),
            json={"allowed_upstreams": ["missing"]},
        )
        assert update_resp.status_code == 400
        assert update_resp.json()["message"] == "Unknown upstream id: missing"


def test_admin_rejects_unknown_or_empty_allowed_endpoints(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "bad-endpoint", "tier": "normal", "allowed_endpoints": ["missing"]},
        )
        assert create_resp.status_code == 400
        assert create_resp.json()["message"] == "Unknown endpoint: missing"

        ok_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "endpoint-user", "tier": "normal", "allowed_endpoints": ["generate-image"]},
        )
        user_id = ok_resp.json()["user_id"]

        update_resp = client.patch(
            f"/admin/api/users/{user_id}",
            auth=("admin", "admin123"),
            json={"allowed_endpoints": []},
        )
        assert update_resp.status_code == 400
        assert update_resp.json()["message"] == "At least one endpoint must be allowed"


def test_admin_missing_user_and_rule_operations_return_404(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        assert client.patch("/admin/api/users/999", auth=("admin", "admin123"), json={"name": "missing"}).status_code == 404
        assert client.delete("/admin/api/users/999", auth=("admin", "admin123")).status_code == 404
        assert client.post("/admin/api/users/999/reset-quota", auth=("admin", "admin123")).status_code == 404
        assert client.post("/admin/api/users/999/rate-limit-rules", auth=("admin", "admin123"), json={"period": "day", "max_requests": 1}).status_code == 404
        assert client.patch("/admin/api/rate-limit-rules/999", auth=("admin", "admin123"), json={"period": "day", "max_requests": 1}).status_code == 404
        assert client.delete("/admin/api/rate-limit-rules/999", auth=("admin", "admin123")).status_code == 404


def test_admin_can_disable_user_access(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "disabled-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        api_key = create_resp.json()["api_key"]

        update_resp = client.patch(
            f"/admin/api/users/{user_id}",
            auth=("admin", "admin123"),
            json={"is_active": False},
        )
        assert update_resp.status_code == 200

        sub_resp = client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"})
        assert sub_resp.status_code == 403


def test_admin_delete_soft_deletes_user_and_preserves_logs(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "delete-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        api_key = create_resp.json()["api_key"]
        client.app.state.db.execute(
            """
            INSERT INTO usage_logs (request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("delete-log", user_id, "generate", 0, "success", "INFO", utc_now_iso()),
        )

        delete_resp = client.delete(f"/admin/api/users/{user_id}", auth=("admin", "admin123"))
        assert delete_resp.status_code == 200

        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        assert all(row["id"] != user_id for row in users)

        old_key_resp = client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"})
        assert old_key_resp.status_code == 401

        user_row = client.app.state.db.query_one("SELECT is_active, deleted_at FROM users WHERE id = ?", (user_id,))
        assert user_row is not None
        assert user_row["is_active"] == 0
        assert user_row["deleted_at"] is not None

        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        preserved = next(row for row in logs if row["request_id"] == "delete-log")
        assert preserved["user_name"] == "delete-user"


def test_deleted_user_queued_request_is_rejected_before_upstream(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    release_event = threading.Event()
    with TestClient(app) as client:
        fake_upstream = BlockingFakeUpstream(release_event)
        client.app.state.upstream = fake_upstream

        first_user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "running-user", "tier": "normal", "anlas_total": 100},
        ).json()
        queued_user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "queued-delete-user", "tier": "normal", "anlas_total": 100},
        ).json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                client.post,
                "/ai/generate-image",
                headers={"Authorization": f"Bearer {first_user['api_key']}"},
                json=PAYLOAD,
            )
            _wait_until(lambda: len(fake_upstream.generate_started_at) == 1)

            second = pool.submit(
                client.post,
                "/ai/generate-image",
                headers={"Authorization": f"Bearer {queued_user['api_key']}"},
                json=PAYLOAD,
            )
            _wait_until(lambda: _is_user_queued(client, queued_user["user_id"]))

            delete_resp = client.delete(f"/admin/api/users/{queued_user['user_id']}", auth=("admin", "admin123"))
            assert delete_resp.status_code == 200
            release_event.set()

            assert first.result(timeout=5).status_code == 201
            rejected = second.result(timeout=5)
            assert rejected.status_code == 403
            assert rejected.json()["message"] == "User is no longer active"

        assert len(fake_upstream.generate_started_at) == 1
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        rejected_log = next(row for row in logs if row["user_id"] == queued_user["user_id"])
        assert rejected_log["status"] == "rejected"
        assert rejected_log["error_code"] == "user_unavailable"


def _wait_until(predicate, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for condition")


def _is_user_queued(client: TestClient, user_id: int) -> bool:
    queue = client.get("/admin/api/queue", auth=("admin", "admin123")).json()
    return any(item["user_id"] == user_id for item in queue["queued"])
