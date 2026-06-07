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


def test_admin_user_group_api_and_create_user_copies_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        group_id = _create_group(
            client,
            name="vip-defaults",
            default_tier="vip",
            default_free_small_only=False,
            default_allowed_endpoints=["generate-image", "upscale"],
            default_anlas_total=123,
            default_reset_period="week",
            default_reset_day=3,
        )

        groups = client.get("/admin/api/user-groups", auth=("admin", "admin123")).json()["groups"]
        created_group = next(row for row in groups if row["id"] == group_id)
        assert created_group["member_count"] == 0
        assert created_group["default_allowed_endpoints_list"] == ["generate-image", "upscale"]

        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "group-user", "group_id": group_id},
        )
        assert create_resp.status_code == 200
        user_id = create_resp.json()["user_id"]

        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        user = next(row for row in users if row["id"] == user_id)
        assert user["group_id"] == group_id
        assert user["tier"] == "vip"
        assert user["free_small_only"] == 0
        assert user["allowed_endpoints_list"] == ["generate-image", "upscale"]
        assert user["anlas_total"] == 123
        quota = client.app.state.db.query_one(
            "SELECT total, reset_period, reset_day FROM user_anlas_quota WHERE user_id = ?",
            (user_id,),
        )
        assert dict(quota) == {"total": 123, "reset_period": "week", "reset_day": 3}

        explicit_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "explicit-user",
                "group_id": group_id,
                "tier": "normal",
                "free_small_only": True,
                "allowed_endpoints": ["generate-image"],
                "anlas_total": 5,
            },
        )
        explicit_user_id = explicit_resp.json()["user_id"]
        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        explicit_user = next(row for row in users if row["id"] == explicit_user_id)
        assert explicit_user["tier"] == "normal"
        assert explicit_user["free_small_only"] == 1
        assert explicit_user["allowed_endpoints_list"] == ["generate-image"]
        assert explicit_user["anlas_total"] == 5


def test_user_group_update_does_not_auto_change_members_and_sync_is_selective(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        group_id = _create_group(
            client,
            name="sync-defaults",
            default_tier="normal",
            default_allowed_endpoints=["generate-image"],
            default_anlas_total=10,
        )
        user_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "member", "group_id": group_id},
        ).json()["user_id"]
        client.app.state.db.execute(
            "UPDATE user_anlas_quota SET used = 3, reserved = 2 WHERE user_id = ?",
            (user_id,),
        )

        patch_resp = client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={
                "default_tier": "vip",
                "default_allowed_endpoints": ["generate-image", "upscale"],
                "default_anlas_total": 50,
            },
        )
        assert patch_resp.status_code == 200

        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        unchanged = next(row for row in users if row["id"] == user_id)
        assert unchanged["tier"] == "normal"
        assert unchanged["allowed_endpoints_list"] == ["generate-image"]
        assert unchanged["anlas_total"] == 10

        sync_tier = client.post(
            f"/admin/api/user-groups/{group_id}/sync-members",
            auth=("admin", "admin123"),
            json={"fields": ["tier"]},
        )
        assert sync_tier.status_code == 200
        assert sync_tier.json()["updated_users"] == 1

        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        tier_only = next(row for row in users if row["id"] == user_id)
        assert tier_only["tier"] == "vip"
        assert tier_only["allowed_endpoints_list"] == ["generate-image"]
        assert tier_only["anlas_total"] == 10

        sync_quota = client.post(
            f"/admin/api/user-groups/{group_id}/sync-members",
            auth=("admin", "admin123"),
            json={"fields": ["allowed_endpoints", "anlas_quota"]},
        )
        assert sync_quota.status_code == 200

        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        synced = next(row for row in users if row["id"] == user_id)
        assert synced["allowed_endpoints_list"] == ["generate-image", "upscale"]
        assert synced["anlas_total"] == 50
        quota = client.app.state.db.query_one(
            "SELECT total, used, reserved FROM user_anlas_quota WHERE user_id = ?",
            (user_id,),
        )
        assert dict(quota) == {"total": 50, "used": 3, "reserved": 2}


def test_disabled_or_missing_user_group_cannot_create_user(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        missing_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "missing-group-user", "group_id": 999},
        )
        assert missing_resp.status_code == 404
        assert missing_resp.json()["message"] == "User group not found"

        group_id = _create_group(client, name="disabled", is_active=False)
        disabled_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "disabled-group-user", "group_id": group_id},
        )
        assert disabled_resp.status_code == 400
        assert disabled_resp.json()["message"] == "User group is disabled"

        client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={"is_active": True},
        )
        ok_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "enabled-group-user", "group_id": group_id},
        )
        assert ok_resp.status_code == 200


def test_admin_update_user_group_and_apply_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        group_id = _create_group(
            client,
            name="apply-defaults",
            default_tier="vip",
            default_free_small_only=True,
            default_allowed_endpoints=["generate-image", "upscale"],
            default_anlas_total=77,
        )
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "plain-user", "tier": "normal", "anlas_total": 2},
        )
        user_id = create_resp.json()["user_id"]

        update_resp = client.patch(
            f"/admin/api/users/{user_id}",
            auth=("admin", "admin123"),
            json={"group_id": group_id, "apply_group_defaults": True, "tier": "normal"},
        )
        assert update_resp.status_code == 200

        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        updated = next(row for row in users if row["id"] == user_id)
        assert updated["group_id"] == group_id
        assert updated["tier"] == "normal"
        assert updated["free_small_only"] == 1
        assert updated["allowed_endpoints_list"] == ["generate-image", "upscale"]
        assert updated["anlas_total"] == 77


def test_admin_user_group_web_pages_create_and_show_fixed_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        create_resp = client.post(
            "/admin/user-groups",
            data={
                "name": "web-group",
                "is_active": "on",
                "default_tier": "vip",
                "default_free_small_only": "on",
                "default_allowed_endpoints": "generate-image",
                "default_anlas_total": "25",
                "default_reset_period": "month",
                "default_reset_day": "1",
            },
            follow_redirects=False,
        )
        assert create_resp.status_code == 303
        assert create_resp.headers["location"] == "/admin/user-groups"

        page = client.get("/admin/user-groups")
        assert page.status_code == 200
        assert "web-group" in page.text
        assert "#1" in page.text

        detail = client.get("/admin/user-groups/1")
        assert detail.status_code == 200
        assert "固定 ID" in detail.text
        assert "#1" in detail.text


def test_admin_user_edit_page_can_change_group(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        group_id = _create_group(client, name="edit-target", default_tier="vip")
        user_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "web-edit-user", "tier": "normal", "anlas_total": 10},
        ).json()["user_id"]

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        detail = client.get(f"/admin/users/{user_id}")
        assert detail.status_code == 200
        assert "所属用户组" in detail.text

        update_resp = client.post(
            f"/admin/users/{user_id}",
            data={
                "name": "web-edit-user",
                "group_id": str(group_id),
                "tier": "normal",
                "is_active": "on",
                "anlas_total": "10",
                "reset_period": "month",
                "reset_day": "1",
                "allowed_endpoints": "generate-image",
            },
            follow_redirects=False,
        )
        assert update_resp.status_code == 303
        assert update_resp.headers["location"] == f"/admin/users/{user_id}"

        user = client.app.state.db.query_one("SELECT group_id FROM users WHERE id = ?", (user_id,))
        assert user["group_id"] == group_id


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


def _create_group(client: TestClient, **overrides) -> int:
    payload = {
        "name": "group",
        "is_active": True,
        "default_tier": "normal",
        "default_free_small_only": True,
        "default_allowed_endpoints": ["generate-image"],
        "default_allowed_upstreams": [],
        "default_anlas_total": 0,
        "default_reset_period": "month",
        "default_reset_day": 1,
    }
    payload.update(overrides)
    response = client.post("/admin/api/user-groups", auth=("admin", "admin123"), json=payload)
    assert response.status_code == 200
    return response.json()["group_id"]
