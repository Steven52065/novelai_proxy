from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import utc_now_iso
from helpers import BlockingFakeUpstream, PAYLOAD, csrf_headers, write_test_config, write_test_config_with_upstreams


def test_admin_create_update_free_small_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "limited",
                "tier": "normal",
                "anlas_total": 100,
                "free_small_only": True,
                "free_small_daily_limit_enabled": True,
                "free_small_daily_limit": 3,
                "image_format_policy": "force_png",
            },
        )
        assert create_resp.status_code == 200
        user_id = create_resp.json()["user_id"]

        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        created = next(row for row in users if row["id"] == user_id)
        assert created["free_small_only"] == 1
        assert created["free_small_daily_limit_enabled"] == 1
        assert created["free_small_daily_limit"] == 3
        assert created["allowed_endpoints"] == "generate-image"
        assert created["allowed_endpoints_list"] == ["generate-image"]
        assert created["image_format_policy"] == "force_png"

        update_resp = client.patch(
            f"/admin/api/users/{user_id}",
            auth=("admin", "admin123"),
            json={
                "free_small_only": False,
                "free_small_daily_limit_enabled": False,
                "free_small_daily_limit": 0,
                "allowed_endpoints": ["generate-image", "upscale"],
                "image_format_policy": "force_webp",
            },
        )
        assert update_resp.status_code == 200
        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        updated = next(row for row in users if row["id"] == user_id)
        assert updated["free_small_only"] == 0
        assert updated["free_small_daily_limit_enabled"] == 0
        assert updated["free_small_daily_limit"] == 0
        assert updated["allowed_endpoints"] == "generate-image,upscale"
        assert updated["allowed_endpoints_list"] == ["generate-image", "upscale"]
        assert updated["image_format_policy"] == "force_webp"


def test_admin_rejects_invalid_image_format_policy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "bad-format-user", "image_format_policy": "gif"},
        )
        assert create_resp.status_code == 400
        assert create_resp.json()["message"] == "Invalid request"

        ok_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "format-user", "image_format_policy": "respect_request"},
        )
        user_id = ok_resp.json()["user_id"]
        update_resp = client.patch(
            f"/admin/api/users/{user_id}",
            auth=("admin", "admin123"),
            json={"image_format_policy": "gif"},
        )
        assert update_resp.status_code == 400
        assert update_resp.json()["message"] == "Invalid request"

        bad_group = client.post(
            "/admin/api/user-groups",
            auth=("admin", "admin123"),
            json={"name": "bad-format-group", "default_image_format_policy": "gif"},
        )
        assert bad_group.status_code == 400
        assert bad_group.json()["message"] == "Invalid request"

        group_id = _create_group(client, name="format-group", default_image_format_policy="force_png")
        bad_group_update = client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={"default_image_format_policy": "gif"},
        )
        assert bad_group_update.status_code == 400
        assert bad_group_update.json()["message"] == "Invalid request"


def test_admin_web_forms_reject_invalid_image_format_policy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        client.headers.update(csrf_headers(client))

        bad_user_create = client.post(
            "/admin/users",
            data={
                "name": "bad-web-format-user",
                "allowed_endpoints": "generate-image",
                "image_format_policy": "gif",
            },
            follow_redirects=False,
        )
        assert bad_user_create.status_code == 400
        assert bad_user_create.json()["message"] == "Unknown image_format_policy: gif"

        user_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "web-format-user", "tier": "normal", "anlas_total": 10},
        ).json()["user_id"]
        bad_user_update = client.post(
            f"/admin/users/{user_id}",
            data={
                "name": "web-format-user",
                "tier": "normal",
                "is_active": "on",
                "anlas_total": "10",
                "reset_period": "month",
                "reset_day": "1",
                "allowed_endpoints": "generate-image",
                "image_format_policy": "gif",
            },
            follow_redirects=False,
        )
        assert bad_user_update.status_code == 400
        assert bad_user_update.json()["message"] == "Unknown image_format_policy: gif"

        bad_group_id = client.post(
            "/admin/users",
            data={
                "name": "bad-group-id-user",
                "group_id": "abc",
                "allowed_endpoints": "generate-image",
            },
            follow_redirects=False,
        )
        assert bad_group_id.status_code == 400
        assert bad_group_id.json()["message"] == "Invalid form value"

        bad_group_create = client.post(
            "/admin/user-groups",
            data={
                "name": "bad-web-format-group",
                "default_allowed_endpoints": "generate-image",
                "default_image_format_policy": "gif",
            },
            follow_redirects=False,
        )
        assert bad_group_create.status_code == 400
        assert bad_group_create.json()["message"] == "Unknown image_format_policy: gif"

        group_id = _create_group(client, name="web-format-group")
        bad_group_update = client.post(
            f"/admin/user-groups/{group_id}",
            data={
                "name": "web-format-group",
                "is_active": "on",
                "default_tier": "normal",
                "default_free_small_only": "on",
                "default_allowed_endpoints": "generate-image",
                "default_anlas_total": "0",
                "default_reset_period": "month",
                "default_reset_day": "1",
                "free_small_daily_limit": "0",
                "default_image_format_policy": "gif",
            },
            follow_redirects=False,
        )
        assert bad_group_update.status_code == 400
        assert bad_group_update.json()["message"] == "Unknown image_format_policy: gif"

        bad_propagated = client.get(f"/admin/user-groups/{group_id}?propagated=abc")
        assert bad_propagated.status_code == 400
        assert bad_propagated.json()["message"] == "Invalid query parameter"


def test_admin_rejects_enabled_free_small_daily_limit_without_positive_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "bad-daily-user", "free_small_daily_limit_enabled": True, "free_small_daily_limit": 0},
        )
        assert create_resp.status_code == 400
        assert create_resp.json()["message"] == "free_small_daily_limit must be >= 1 when enabled"

        group_resp = client.post(
            "/admin/api/user-groups",
            auth=("admin", "admin123"),
            json={"name": "bad-daily-group", "free_small_daily_limit_enabled": True, "free_small_daily_limit": 0},
        )
        assert group_resp.status_code == 400
        assert group_resp.json()["message"] == "free_small_daily_limit must be >= 1 when enabled"

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
        client.headers.update(csrf_headers(client))

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


def test_admin_user_pages_show_daily_usage_and_hide_zero_anlas_usage(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        group_id = _create_group(
            client,
            name="daily-group",
            free_small_daily_limit_enabled=True,
            free_small_daily_limit=5,
            default_anlas_total=0,
        )
        zero_anlas_user_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "daily-zero-anlas", "group_id": group_id},
        ).json()["user_id"]
        paid_user_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "paid-anlas", "tier": "normal", "anlas_total": 12},
        ).json()["user_id"]
        client.app.state.db.execute(
            "UPDATE user_anlas_quota SET used = 3, reserved = 2 WHERE user_id = ?",
            (paid_user_id,),
        )

        daily_manager = client.app.state.free_small_daily_limit_manager
        used_reservation = daily_manager.reserve(zero_anlas_user_id, 2)
        daily_manager.confirm(used_reservation)
        daily_manager.reserve(zero_anlas_user_id, 1)

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        users_page = client.get("/admin/users")
        assert users_page.status_code == 200
        users_text = _normalized_text(users_page.text)
        assert "API 密钥凭证" not in users_text
        assert "需在编辑中重置" not in users_text
        assert "每日 5 张" in users_text
        assert "可用 2 张" in users_text
        assert "免费小图单日数量：" not in users_text
        assert "2 + 1 / 5" not in users_text
        assert "未配置Anlas额度" in users_text
        assert "0 + 0 / 0" not in users_text
        assert "3 + 2 / 12" in users_text

        zero_detail = client.get(f"/admin/users/{zero_anlas_user_id}")
        assert zero_detail.status_code == 200
        zero_detail_text = _normalized_text(zero_detail.text)
        assert "免费小图单日数量" in zero_detail_text
        assert "已用 2 锁定 1 上限 5 可用 2" in zero_detail_text
        assert "Anlas额度使用状态" not in zero_detail_text
        assert "Anlas额度总额上限" in zero_detail_text

        paid_detail = client.get(f"/admin/users/{paid_user_id}")
        assert paid_detail.status_code == 200
        assert "Anlas额度使用状态" in _normalized_text(paid_detail.text)


def test_admin_user_list_is_paginated_and_filters_server_side(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        db = client.app.state.db
        now = utc_now_iso()
        with db.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO users (api_key_hash, name, tier, is_active, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"page-hash-{index}",
                        f"page-user-{index:04d}",
                        "vip" if index % 2 == 0 else "normal",
                        0 if index % 5 == 0 else 1,
                        now,
                    )
                    for index in range(1500)
                ],
            )

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        first_page = client.get("/admin/users")
        assert first_page.status_code == 200
        assert first_page.text.count("<tr data-user-row") == 50
        assert 'data-user-name="page-user-0000"' in first_page.text
        assert 'data-user-name="page-user-0050"' not in first_page.text
        assert "第 1 / 30 页" in first_page.text
        assert "users-load-sentinel" in first_page.text
        assert "IntersectionObserver" in first_page.text

        second_page = client.get("/admin/users?page=2")
        assert second_page.status_code == 200
        assert 'data-user-name="page-user-0050"' in second_page.text
        assert 'data-user-name="page-user-0000"' not in second_page.text
        assert "page=1" in second_page.text

        search_page = client.get("/admin/users?q=page-user-1499")
        assert search_page.status_code == 200
        assert search_page.text.count("<tr data-user-row") == 1
        assert 'data-user-name="page-user-1499"' in search_page.text
        assert 'data-user-name="page-user-0000"' not in search_page.text
        assert "第 1 / 1 页" in search_page.text

        filtered_page = client.get("/admin/users?tier=vip&status=inactive")
        assert filtered_page.status_code == 200
        assert filtered_page.text.count("<tr data-user-row") == 50
        assert 'data-user-name="page-user-0000"' in filtered_page.text
        assert 'data-user-name="page-user-0002"' not in filtered_page.text
        assert 'data-user-name="page-user-0005"' not in filtered_page.text
        assert "第 1 / 3 页" in filtered_page.text


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
            free_small_daily_limit_enabled=True,
            free_small_daily_limit=5,
            default_allowed_endpoints=["generate-image", "upscale"],
            default_image_format_policy="force_png",
            default_anlas_total=123,
            default_reset_period="week",
            default_reset_day=3,
        )

        groups = client.get("/admin/api/user-groups", auth=("admin", "admin123")).json()["groups"]
        created_group = next(row for row in groups if row["id"] == group_id)
        assert created_group["member_count"] == 0
        assert created_group["free_small_daily_limit_enabled"] == 1
        assert created_group["free_small_daily_limit"] == 5
        assert created_group["default_allowed_endpoints_list"] == ["generate-image", "upscale"]
        assert created_group["default_image_format_policy"] == "force_png"

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
        assert user["free_small_daily_limit_enabled"] == 1
        assert user["free_small_daily_limit"] == 5
        assert user["allowed_endpoints_list"] == ["generate-image", "upscale"]
        assert user["image_format_policy"] == "force_png"
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
                "image_format_policy": "respect_request",
                "anlas_total": 5,
            },
        )
        explicit_user_id = explicit_resp.json()["user_id"]
        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        explicit_user = next(row for row in users if row["id"] == explicit_user_id)
        assert explicit_user["tier"] == "normal"
        assert explicit_user["free_small_only"] == 1
        assert explicit_user["allowed_endpoints_list"] == ["generate-image"]
        assert explicit_user["image_format_policy"] == "respect_request"
        assert explicit_user["anlas_total"] == 5


def test_user_group_update_propagates_to_following_members_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        group_id = _create_group(
            client,
            name="propagate-defaults",
            default_tier="normal",
            default_allowed_endpoints=["generate-image"],
            default_image_format_policy="follow_global",
            default_anlas_total=10,
        )
        follower_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "follower", "group_id": group_id},
        ).json()["user_id"]
        modified_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "modified",
                "group_id": group_id,
                "anlas_total": 5,
                "free_small_daily_limit_enabled": True,
                "free_small_daily_limit": 7,
                "image_format_policy": "respect_request",
            },
        ).json()["user_id"]
        client.app.state.db.execute(
            "UPDATE user_anlas_quota SET used = 3, reserved = 2 WHERE user_id = ?",
            (follower_id,),
        )

        patch_resp = client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={
                "default_tier": "vip",
                "default_allowed_endpoints": ["generate-image", "upscale"],
                "default_image_format_policy": "force_webp",
                "default_anlas_total": 50,
                "free_small_daily_limit_enabled": True,
                "free_small_daily_limit": 3,
            },
        )
        assert patch_resp.status_code == 200
        summary = patch_resp.json()["propagation"]
        assert summary["propagate_scope"] == "unmodified"
        assert summary["member_count"] == 2
        assert summary["updated_users"] == 2
        field_updates = {item["field"]: item["updated"] for item in summary["fields"]}
        assert field_updates == {
            "tier": 2,
            "free_small_daily_limit": 1,
            "allowed_endpoints": 2,
            "image_format_policy": 1,
            "anlas_quota": 1,
        }

        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        follower = next(row for row in users if row["id"] == follower_id)
        assert follower["tier"] == "vip"
        assert follower["allowed_endpoints_list"] == ["generate-image", "upscale"]
        assert follower["image_format_policy"] == "force_webp"
        assert follower["anlas_total"] == 50
        assert follower["free_small_daily_limit_enabled"] == 1
        assert follower["free_small_daily_limit"] == 3
        quota = client.app.state.db.query_one(
            "SELECT total, used, reserved FROM user_anlas_quota WHERE user_id = ?",
            (follower_id,),
        )
        assert dict(quota) == {"total": 50, "used": 3, "reserved": 2}

        modified = next(row for row in users if row["id"] == modified_id)
        assert modified["tier"] == "vip"
        assert modified["anlas_total"] == 5
        assert modified["free_small_daily_limit_enabled"] == 1
        assert modified["free_small_daily_limit"] == 7
        assert modified["allowed_endpoints_list"] == ["generate-image", "upscale"]
        assert modified["image_format_policy"] == "respect_request"


def test_user_group_update_propagate_scopes_none_all_and_preview(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        group_id = _create_group(
            client,
            name="scope-group",
            default_anlas_total=10,
            default_image_format_policy="force_png",
        )
        follower_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "scope-follower", "group_id": group_id},
        ).json()["user_id"]
        modified_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "scope-modified",
                "group_id": group_id,
                "anlas_total": 5,
                "image_format_policy": "respect_request",
            },
        ).json()["user_id"]

        preview = client.post(
            f"/admin/api/user-groups/{group_id}/propagation-preview",
            auth=("admin", "admin123"),
            json={"default_anlas_total": 50, "default_image_format_policy": "force_webp"},
        )
        assert preview.status_code == 200
        preview_data = preview.json()
        assert preview_data["member_count"] == 2
        field_preview = {field["field"]: field for field in preview_data["fields"]}
        assert set(field_preview) == {"image_format_policy", "anlas_quota"}
        assert field_preview["image_format_policy"]["unmodified_count"] == 1
        assert field_preview["anlas_quota"]["unmodified_count"] == 1

        no_change_preview = client.post(
            f"/admin/api/user-groups/{group_id}/propagation-preview",
            auth=("admin", "admin123"),
            json={"default_anlas_total": 10, "default_image_format_policy": "force_png"},
        )
        assert no_change_preview.status_code == 200
        assert no_change_preview.json()["fields"] == []

        none_resp = client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={"default_anlas_total": 50, "default_image_format_policy": "force_webp", "propagate": "none"},
        )
        assert none_resp.status_code == 200
        assert none_resp.json()["propagation"]["updated_users"] == 0
        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        assert next(row for row in users if row["id"] == follower_id)["anlas_total"] == 10
        assert next(row for row in users if row["id"] == modified_id)["anlas_total"] == 5
        assert next(row for row in users if row["id"] == follower_id)["image_format_policy"] == "force_png"
        assert next(row for row in users if row["id"] == modified_id)["image_format_policy"] == "respect_request"
        group = client.get(f"/admin/api/user-groups/{group_id}", auth=("admin", "admin123")).json()["group"]
        assert group["default_anlas_total"] == 50
        assert group["default_image_format_policy"] == "force_webp"

        all_resp = client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={"default_anlas_total": 80, "default_image_format_policy": "force_png", "propagate": "all"},
        )
        assert all_resp.status_code == 200
        assert all_resp.json()["propagation"]["updated_users"] == 2
        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        assert next(row for row in users if row["id"] == follower_id)["anlas_total"] == 80
        assert next(row for row in users if row["id"] == modified_id)["anlas_total"] == 80
        assert next(row for row in users if row["id"] == follower_id)["image_format_policy"] == "force_png"
        assert next(row for row in users if row["id"] == modified_id)["image_format_policy"] == "force_png"


def test_user_group_sync_members_is_selective(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        group_id = _create_group(
            client,
            name="sync-defaults",
            default_tier="normal",
            default_allowed_endpoints=["generate-image"],
            default_image_format_policy="follow_global",
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
                "default_image_format_policy": "force_webp",
                "default_anlas_total": 50,
                "free_small_daily_limit_enabled": True,
                "free_small_daily_limit": 4,
                "propagate": "none",
            },
        )
        assert patch_resp.status_code == 200

        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        unchanged = next(row for row in users if row["id"] == user_id)
        assert unchanged["tier"] == "normal"
        assert unchanged["allowed_endpoints_list"] == ["generate-image"]
        assert unchanged["image_format_policy"] == "follow_global"
        assert unchanged["anlas_total"] == 10
        assert unchanged["free_small_daily_limit_enabled"] == 0

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
        assert tier_only["image_format_policy"] == "follow_global"
        assert tier_only["anlas_total"] == 10

        sync_quota = client.post(
            f"/admin/api/user-groups/{group_id}/sync-members",
            auth=("admin", "admin123"),
            json={"fields": ["allowed_endpoints", "image_format_policy", "anlas_quota", "free_small_daily_limit"]},
        )
        assert sync_quota.status_code == 200

        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        synced = next(row for row in users if row["id"] == user_id)
        assert synced["allowed_endpoints_list"] == ["generate-image", "upscale"]
        assert synced["image_format_policy"] == "force_webp"
        assert synced["anlas_total"] == 50
        assert synced["free_small_daily_limit_enabled"] == 1
        assert synced["free_small_daily_limit"] == 4
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
            default_image_format_policy="force_webp",
            default_anlas_total=77,
        )
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "plain-user", "tier": "normal", "anlas_total": 2, "image_format_policy": "respect_request"},
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
        assert updated["image_format_policy"] == "force_webp"
        assert updated["anlas_total"] == 77


def test_reset_day_validation_matches_reset_period(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        bad_week_user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "bad-week-user", "reset_period": "week", "reset_day": 8},
        )
        assert bad_week_user.status_code == 400
        assert "reset_day must be between 1 and 7" in str(bad_week_user.json())

        bad_month_user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "bad-month-user", "reset_period": "month", "reset_day": 0},
        )
        assert bad_month_user.status_code == 400
        assert "reset_day must be between 1 and 28" in str(bad_month_user.json())

        day_user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "day-user", "reset_period": "day", "reset_day": 28},
        )
        assert day_user.status_code == 200
        user_id = day_user.json()["user_id"]
        quota = client.app.state.db.query_one(
            "SELECT reset_period, reset_day FROM user_anlas_quota WHERE user_id = ?",
            (user_id,),
        )
        assert dict(quota) == {"reset_period": "day", "reset_day": 0}

        bad_week_update = client.patch(
            f"/admin/api/users/{user_id}",
            auth=("admin", "admin123"),
            json={"reset_period": "week", "reset_day": 8},
        )
        assert bad_week_update.status_code == 400
        assert "reset_day must be between 1 and 7" in str(bad_week_update.json())

        week_update = client.patch(
            f"/admin/api/users/{user_id}",
            auth=("admin", "admin123"),
            json={"reset_period": "week"},
        )
        assert week_update.status_code == 200
        quota = client.app.state.db.query_one(
            "SELECT reset_period, reset_day FROM user_anlas_quota WHERE user_id = ?",
            (user_id,),
        )
        assert quota["reset_period"] == "week"
        assert 1 <= quota["reset_day"] <= 7

        bad_week_group = client.post(
            "/admin/api/user-groups",
            auth=("admin", "admin123"),
            json={
                "name": "bad-week-group",
                "default_reset_period": "week",
                "default_reset_day": 8,
            },
        )
        assert bad_week_group.status_code == 400
        assert "reset_day must be between 1 and 7" in str(bad_week_group.json())

        never_group_id = _create_group(
            client,
            name="never-group",
            default_reset_period="never",
            default_reset_day=28,
        )
        group = client.app.state.db.query_one("SELECT default_reset_period, default_reset_day FROM user_groups WHERE id = ?", (never_group_id,))
        assert dict(group) == {"default_reset_period": "never", "default_reset_day": 0}


def test_admin_user_group_web_pages_create_and_show_fixed_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        client.headers.update(csrf_headers(client))

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


def test_admin_user_group_web_form_save_propagates_with_scope(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        group_id = _create_group(client, name="web-propagate", default_anlas_total=10)
        follower_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "web-follower", "group_id": group_id},
        ).json()["user_id"]

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        client.headers.update(csrf_headers(client))

        page = client.get(f"/admin/user-groups/{group_id}")
        assert page.status_code == 200
        assert "propagate-modal" in page.text
        assert "propagation-preview" in page.text

        preview = client.post(
            f"/admin/api/user-groups/{group_id}/propagation-preview",
            json={"default_tier": "vip"},
        )
        assert preview.status_code == 200
        preview_data = preview.json()
        assert preview_data["member_count"] == 1
        assert [field["field"] for field in preview_data["fields"]] == ["tier"]

        update_resp = client.post(
            f"/admin/user-groups/{group_id}",
            data={
                "name": "web-propagate",
                "is_active": "on",
                "default_tier": "vip",
                "default_free_small_only": "on",
                "default_allowed_endpoints": "generate-image",
                "default_anlas_total": "10",
                "default_reset_period": "month",
                "default_reset_day": "1",
                "free_small_daily_limit": "0",
                "propagate_scope": "unmodified",
            },
            follow_redirects=False,
        )
        assert update_resp.status_code == 303
        location = update_resp.headers["location"]
        assert location.startswith(f"/admin/user-groups/{group_id}?saved=1")
        assert "propagated=1" in location

        result_page = client.get(location)
        assert result_page.status_code == 200
        assert "已保存组默认配置" in result_page.text

        user = client.app.state.db.query_one("SELECT tier FROM users WHERE id = ?", (follower_id,))
        assert user["tier"] == "vip"


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
        client.headers.update(csrf_headers(client))
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


def test_admin_user_group_rate_limit_rules_api_and_page(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        group_id = _create_group(client, name="rate-group")
        add_resp = client.post(
            f"/admin/api/user-groups/{group_id}/rate-limit-rules",
            auth=("admin", "admin123"),
            json={"period": "minute", "max_requests": 2},
        )
        assert add_resp.status_code == 200
        rule = client.app.state.db.query_one(
            "SELECT id, period, max_requests, is_active FROM group_rate_limit_rules WHERE group_id = ?",
            (group_id,),
        )
        assert rule["period"] == "minute"
        assert rule["max_requests"] == 2

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        page = client.get(f"/admin/user-groups/{group_id}")
        assert page.status_code == 200
        assert "组共享限流" in page.text
        assert "minute" in page.text

        patch_resp = client.patch(
            f"/admin/api/group-rate-limit-rules/{rule['id']}",
            auth=("admin", "admin123"),
            json={"period": "hour", "max_requests": 3, "is_active": False},
        )
        assert patch_resp.status_code == 200
        updated = client.app.state.db.query_one(
            "SELECT period, max_requests, is_active FROM group_rate_limit_rules WHERE id = ?",
            (rule["id"],),
        )
        assert dict(updated) == {"period": "hour", "max_requests": 3, "is_active": 0}

        delete_resp = client.delete(
            f"/admin/api/group-rate-limit-rules/{rule['id']}",
            auth=("admin", "admin123"),
        )
        assert delete_resp.status_code == 200
        assert client.app.state.db.query_one("SELECT id FROM group_rate_limit_rules WHERE id = ?", (rule["id"],)) is None


def test_group_member_rate_limit_rules_propagate_by_scope(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        group_id = _create_group(client, name="member-rate-group")
        follower_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "rate-follower", "group_id": group_id},
        ).json()["user_id"]
        modified_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "rate-modified", "group_id": group_id},
        ).json()["user_id"]
        # 手动给一个成员配置规则，使其不再跟随组模板。
        client.post(
            f"/admin/api/users/{modified_id}/rate-limit-rules",
            auth=("admin", "admin123"),
            json={"period": "hour", "max_requests": 99},
        )

        preview = client.post(
            f"/admin/api/user-groups/{group_id}/propagation-preview",
            auth=("admin", "admin123"),
            json={"member_rate_limit_rules": [{"period": "minute", "max_requests": 5}]},
        )
        assert preview.status_code == 200
        preview_field = next(
            field for field in preview.json()["fields"] if field["field"] == "member_rate_limit_rules"
        )
        assert preview_field["old"] == "未配置（不限频）"
        assert preview_field["new"] == "每分钟 5 次"
        assert preview_field["unmodified_count"] == 1

        patch_resp = client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={"member_rate_limit_rules": [{"period": "minute", "max_requests": 5}]},
        )
        assert patch_resp.status_code == 200
        summary = patch_resp.json()["propagation"]
        field_updates = {item["field"]: item["updated"] for item in summary["fields"]}
        assert field_updates == {"member_rate_limit_rules": 1}

        assert _user_rules(client, follower_id) == [("minute", 5, 1)]
        assert _user_rules(client, modified_id) == [("hour", 99, 1)]

        # 覆盖全部成员时，手动改过的成员也被拉回组模板。
        all_resp = client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={
                "member_rate_limit_rules": [
                    {"period": "minute", "max_requests": 7},
                    {"period": "day", "max_requests": 100, "is_active": False},
                ],
                "propagate": "all",
            },
        )
        assert all_resp.status_code == 200
        assert all_resp.json()["propagation"]["updated_users"] == 2
        expected = [("minute", 7, 1), ("day", 100, 0)]
        assert _user_rules(client, follower_id) == expected
        assert _user_rules(client, modified_id) == expected

        # 只保存组配置时成员不受影响。
        none_resp = client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={
                "member_rate_limit_rules": [{"period": "minute", "max_requests": 1}],
                "propagate": "none",
            },
        )
        assert none_resp.status_code == 200
        assert none_resp.json()["propagation"]["updated_users"] == 0
        assert _user_rules(client, follower_id) == expected

        # 清空组模板并覆盖全部成员。
        clear_resp = client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={"member_rate_limit_rules": [], "propagate": "all"},
        )
        assert clear_resp.status_code == 200
        assert _user_rules(client, follower_id) == []
        assert _user_rules(client, modified_id) == []
        assert (
            client.app.state.db.query_one(
                "SELECT COUNT(*) AS total FROM group_member_rate_limit_rules WHERE group_id = ?",
                (group_id,),
            )["total"]
            == 0
        )


def test_group_member_rate_limit_rules_sync_and_inherit(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        group_id = _create_group(client, name="member-rate-sync")
        existing_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "sync-existing", "group_id": group_id},
        ).json()["user_id"]

        client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={
                "member_rate_limit_rules": [{"period": "minute", "max_requests": 4}],
                "propagate": "none",
            },
        )
        assert _user_rules(client, existing_id) == []

        # 新建成员默认继承组模板。
        inheritor_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "sync-inheritor", "group_id": group_id},
        ).json()["user_id"]
        assert _user_rules(client, inheritor_id) == [("minute", 4, 1)]

        # 同步到成员：强制覆盖全部成员。
        sync_resp = client.post(
            f"/admin/api/user-groups/{group_id}/sync-members",
            auth=("admin", "admin123"),
            json={"fields": ["member_rate_limit_rules"]},
        )
        assert sync_resp.status_code == 200
        # sync-members 是无条件强制覆盖，已经与模板一致的成员也计入。
        assert sync_resp.json()["updated_users"] == 2
        assert _user_rules(client, existing_id) == [("minute", 4, 1)]

        # 手动改过之后，勾选套用组默认值会把规则整体拉回组模板。
        client.post(
            f"/admin/api/users/{existing_id}/rate-limit-rules",
            auth=("admin", "admin123"),
            json={"period": "day", "max_requests": 500},
        )
        assert len(_user_rules(client, existing_id)) == 2
        apply_resp = client.patch(
            f"/admin/api/users/{existing_id}",
            auth=("admin", "admin123"),
            json={"group_id": group_id, "apply_group_defaults": True},
        )
        assert apply_resp.status_code == 200
        assert _user_rules(client, existing_id) == [("minute", 4, 1)]


def test_group_member_rate_limit_rules_reject_invalid_payloads(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        group_id = _create_group(client, name="member-rate-invalid")

        duplicated = client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={
                "member_rate_limit_rules": [
                    {"period": "minute", "max_requests": 5},
                    {"period": "minute", "max_requests": 6},
                ]
            },
        )
        assert duplicated.status_code == 400
        assert "Duplicated" in duplicated.json()["message"]

        bad_period = client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={"member_rate_limit_rules": [{"period": "year", "max_requests": 5}]},
        )
        assert bad_period.status_code == 400

        zero_limit = client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={"member_rate_limit_rules": [{"period": "minute", "max_requests": 0}]},
        )
        assert zero_limit.status_code == 400

        too_many = client.patch(
            f"/admin/api/user-groups/{group_id}",
            auth=("admin", "admin123"),
            json={
                "member_rate_limit_rules": [
                    {"period": "minute", "max_requests": index + 1} for index in range(9)
                ]
            },
        )
        assert too_many.status_code == 400


def test_group_member_rate_limit_rules_web_form_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        group_id = _create_group(client, name="member-rate-web")
        member_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "web-rate-member", "group_id": group_id},
        ).json()["user_id"]

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        client.headers.update(csrf_headers(client))

        page = client.get(f"/admin/user-groups/{group_id}")
        assert page.status_code == 200
        assert "组内每人限频" in page.text
        assert "member_rules_submitted" in page.text

        base_form = {
            "name": "member-rate-web",
            "is_active": "on",
            "default_tier": "normal",
            "default_free_small_only": "on",
            "default_allowed_endpoints": "generate-image",
            "default_anlas_total": "0",
            "default_reset_period": "month",
            "default_reset_day": "1",
            "free_small_daily_limit": "0",
            "member_rules_submitted": "1",
            "propagate_scope": "all",
        }

        save_resp = client.post(
            f"/admin/user-groups/{group_id}",
            data={
                **base_form,
                "member_rule_period": ["minute", "hour"],
                "member_rule_max_requests": ["3", "40"],
                "member_rule_active": ["on", "off"],
            },
            follow_redirects=False,
        )
        assert save_resp.status_code == 303
        assert _user_rules(client, member_id) == [("minute", 3, 1), ("hour", 40, 0)]

        # 哨兵存在但没有任何规则行 = 清空规则，而不是「不修改」。
        clear_resp = client.post(
            f"/admin/user-groups/{group_id}",
            data=base_form,
            follow_redirects=False,
        )
        assert clear_resp.status_code == 303
        assert _user_rules(client, member_id) == []

        misaligned = client.post(
            f"/admin/user-groups/{group_id}",
            data={
                **base_form,
                "member_rule_period": ["minute", "hour"],
                "member_rule_max_requests": ["3"],
                "member_rule_active": ["on", "on"],
            },
            follow_redirects=False,
        )
        assert misaligned.status_code == 400
        assert misaligned.json()["message"] == "Rate limit rule fields are misaligned"


def _user_rules(client: TestClient, user_id: int) -> list[tuple[str, int, int]]:
    rows = client.app.state.db.query_all(
        "SELECT period, max_requests, is_active FROM rate_limit_rules WHERE user_id = ? ORDER BY id",
        (user_id,),
    )
    return [(row["period"], row["max_requests"], row["is_active"]) for row in rows]


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


def _normalized_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def _create_group(client: TestClient, **overrides) -> int:
    payload = {
        "name": "group",
        "is_active": True,
        "default_tier": "normal",
        "default_free_small_only": True,
        "default_allowed_endpoints": ["generate-image"],
        "default_allowed_upstreams": [],
        "default_image_format_policy": "follow_global",
        "default_anlas_total": 0,
        "default_reset_period": "month",
        "default_reset_day": 1,
    }
    payload.update(overrides)
    response = client.post("/admin/api/user-groups", auth=("admin", "admin123"), json=payload)
    assert response.status_code == 200
    return response.json()["group_id"]
