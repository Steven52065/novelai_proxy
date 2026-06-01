from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from helpers import write_test_config, write_test_config_with_upstreams


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

def test_admin_can_copy_and_reset_user_key(tmp_path: Path, monkeypatch):
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
        assert created["api_key"] == old_key

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
        assert updated["api_key"] == new_key


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
