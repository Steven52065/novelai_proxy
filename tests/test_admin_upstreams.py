from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from helpers import PAYLOAD, FakeUpstream, write_test_config, write_test_config_with_upstreams


def test_admin_upstreams_list_masks_tokens(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        client.app.state.db.execute(
            "UPDATE novelai_upstreams SET updated_at = ? WHERE id = ?",
            ("2026-06-28T01:02:03.987654+00:00", "default"),
        )
        resp = client.get("/admin/api/upstreams", auth=("admin", "admin123"))

        assert resp.status_code == 200
        text = resp.text
        assert "pst-test-token-default" not in text
        upstream = resp.json()["upstreams"][0]
        assert upstream["api_key_masked"].startswith("pst-te")
        assert upstream["changed_at_display"] == "2026-06-28 09:02:03 UTC+8"

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        page = client.get("/admin/upstreams")
        assert page.status_code == 200
        assert "2026-06-28 09:02:03 UTC+8" in page.text
        assert "2026-06-28T01:02:03.987654+00:00</td>" not in page.text
        assert "upstream-switch-input" in page.text


def test_admin_upstream_update_replaces_runtime_client_without_restarting(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        before = app.state.upstream_clients["default"]
        resp = client.patch(
            "/admin/api/upstreams/default",
            auth=("admin", "admin123"),
            json={"api_key": "pst-new-runtime-token"},
        )

        assert resp.status_code == 200
        assert "pst-new-runtime-token" not in resp.text
        assert app.state.upstream_clients["default"].api_key == "pst-new-runtime-token"
        assert app.state.upstream_clients["default"] is not before


def test_upstream_runtime_client_provider_reads_current_app_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"])))
    from app.main import app

    with TestClient(app):
        runtime = app.state.upstream_runtime
        default_provider = runtime.client_provider_for("opus-a")
        secondary_provider = runtime.client_provider_for("opus-b")
        default_fake = FakeUpstream()
        secondary_fake = FakeUpstream()

        app.state.upstream = default_fake
        app.state.upstream_clients["opus-b"] = secondary_fake

        assert app.state.default_upstream_id == "opus-a"
        assert default_provider() is default_fake
        assert secondary_provider() is secondary_fake


def test_admin_upstream_create_registers_queue_target_immediately(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        created = client.post(
            "/admin/api/upstreams",
            auth=("admin", "admin123"),
            json={"id": "opus-b", "api_key": "pst-opus-b", "enabled": True},
        )
        assert created.status_code == 200

        fake = FakeUpstream()
        app.state.upstream_clients["opus-b"] = fake
        user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "routed", "anlas_total": 100, "allowed_upstreams": ["opus-b"]},
        ).json()

        resp = client.post(
            "/ai/generate-image",
            headers={"Authorization": f"Bearer {user['api_key']}"},
            json=PAYLOAD,
        )

        assert resp.status_code == 201
        assert fake.last_generate_payload == PAYLOAD


def test_admin_upstream_id_accepts_config_style_arbitrary_characters(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    special_id = "账号/一,二 [] !"
    encoded_id = quote(special_id, safe="")

    with TestClient(app) as client:
        created = client.post(
            "/admin/api/upstreams",
            auth=("admin", "admin123"),
            json={"id": f"  {special_id}  ", "api_key": "pst-special-token", "enabled": True},
        )
        assert created.status_code == 200
        assert created.json()["upstream"]["id"] == special_id

        fake = FakeUpstream()
        app.state.upstream_clients[special_id] = fake

        user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "special-routed", "anlas_total": 100, "allowed_upstreams": [special_id]},
        ).json()
        generated = client.post(
            "/ai/generate-image",
            headers={"Authorization": f"Bearer {user['api_key']}"},
            json=PAYLOAD,
        )
        assert generated.status_code == 201
        assert fake.last_generate_payload == PAYLOAD

        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        created_user = next(row for row in users if row["name"] == "special-routed")
        assert created_user["allowed_upstreams"] == '["账号/一,二 [] !"]'
        assert created_user["allowed_upstreams_list"] == [special_id]

        probe = client.post(f"/admin/api/upstreams/{encoded_id}/test", auth=("admin", "admin123"))
        assert probe.status_code == 200
        assert probe.json()["upstream_id"] == special_id

        patched = client.patch(
            f"/admin/api/upstreams/{encoded_id}",
            auth=("admin", "admin123"),
            json={"api_key": "pst-updated-special-token"},
        )
        assert patched.status_code == 200
        assert patched.json()["upstream"]["id"] == special_id
        assert "pst-updated-special-token" not in patched.text

        deletable_id = "可删除/ID,二"
        deletable_encoded_id = quote(deletable_id, safe="")
        assert client.post(
            "/admin/api/upstreams",
            auth=("admin", "admin123"),
            json={"id": deletable_id, "api_key": "pst-deletable-token", "enabled": True},
        ).status_code == 200
        deleted = client.delete(f"/admin/api/upstreams/{deletable_encoded_id}", auth=("admin", "admin123"))
        assert deleted.status_code == 200


def test_admin_upstream_disable_removes_it_from_new_routing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        resp = client.patch(
            "/admin/api/upstreams/default",
            auth=("admin", "admin123"),
            json={"enabled": False},
        )
        assert resp.status_code == 200
        assert app.state.upstream_clients == {}

        user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "no-upstream", "anlas_total": 100},
        ).json()
        generated = client.post(
            "/ai/generate-image",
            headers={"Authorization": f"Bearer {user['api_key']}"},
            json=PAYLOAD,
        )

        assert generated.status_code == 503
        assert generated.json()["message"] == "No enabled upstream is available for this user"


def test_admin_upstream_delete_conflicts_when_referenced(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "bound", "anlas_total": 100, "allowed_upstreams": ["default"]},
        )
        assert user.status_code == 200

        resp = client.delete("/admin/api/upstreams/default", auth=("admin", "admin123"))

        assert resp.status_code == 409
        assert resp.json()["references"]["users"]


def test_admin_upstream_domain_errors_return_json(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        duplicate = client.post(
            "/admin/api/upstreams",
            auth=("admin", "admin123"),
            json={"id": "default", "api_key": "pst-duplicate"},
        )
        missing_update = client.patch(
            "/admin/api/upstreams/missing",
            auth=("admin", "admin123"),
            json={"enabled": False},
        )
        missing_delete = client.delete("/admin/api/upstreams/missing", auth=("admin", "admin123"))
        invalid = client.post(
            "/admin/api/upstreams",
            auth=("admin", "admin123"),
            json={"id": "new-upstream", "api_key": " "},
        )

        assert duplicate.status_code == 409
        assert duplicate.json()["message"] == "upstream id already exists: default"
        assert missing_update.status_code == 404
        assert missing_update.json()["message"] == "Upstream not found"
        assert missing_delete.status_code == 404
        assert missing_delete.json()["message"] == "Upstream not found"
        assert invalid.status_code == 400
        assert invalid.json()["message"] == "api_key must not be empty"


def test_novelai_settings_drive_proxy_costing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        settings = client.patch(
            "/admin/api/novelai-settings",
            auth=("admin", "admin123"),
            json={"account_tier": 1, "upscale_anlas_cost": 7},
        )
        assert settings.status_code == 200

        user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "costed",
                "tier": "vip",
                "anlas_total": 100,
                "allowed_endpoints": ["generate-image", "upscale"],
            },
        ).json()
        headers = {"Authorization": f"Bearer {user['api_key']}"}

        generated = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        upscaled = client.post(
            "/ai/upscale",
            headers=headers,
            json={"image": "aW1n", "width": 64, "height": 64, "scale": 2},
        )

        assert generated.status_code == 201
        assert upscaled.status_code == 201
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        costs = {row["action"]: row["estimated_anlas_cost"] for row in logs if row["status"] == "success"}
        assert costs["generate"] == 17
        # Upscale is priced from the request dimensions and subscription tier;
        # the legacy admin setting no longer overrides the frontend table.
        assert costs["upscale"] == 1
