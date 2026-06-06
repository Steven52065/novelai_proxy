from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from helpers import PAYLOAD, FakeUpstream, write_test_config_with_image_format_policy


def test_generate_respects_requested_official_image_format(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_image_format_policy(tmp_path, "request")))
    from app.main import app

    with TestClient(app) as client:
        fake_upstream = FakeUpstream()
        app.state.upstream = fake_upstream
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "format-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        payload = PAYLOAD | {"parameters": PAYLOAD["parameters"] | {"image_format": "jpeg"}}
        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=payload)

        assert resp.status_code == 201
        assert fake_upstream.last_generate_payload["parameters"]["image_format"] == "jpeg"
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        success_log = next(row for row in logs if row["status"] == "success")
        assert success_log["request_payload"]["parameters"]["image_format"] == "jpeg"

def test_generate_does_not_inject_image_format_in_request_mode(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_image_format_policy(tmp_path, "request")))
    from app.main import app

    with TestClient(app) as client:
        fake_upstream = FakeUpstream()
        app.state.upstream = fake_upstream
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "format-default-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

        assert resp.status_code == 201
        assert "image_format" not in fake_upstream.last_generate_payload["parameters"]

def test_generate_can_force_official_image_format(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_image_format_policy(tmp_path, "force", "webp")))
    from app.main import app

    with TestClient(app) as client:
        fake_upstream = FakeUpstream()
        app.state.upstream = fake_upstream
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "format-force-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        payload = PAYLOAD | {"parameters": PAYLOAD["parameters"] | {"image_format": "png"}}
        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=payload)

        assert resp.status_code == 201
        assert fake_upstream.last_generate_payload["parameters"]["image_format"] == "webp"
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        success_log = next(row for row in logs if row["status"] == "success")
        assert success_log["request_payload"]["parameters"]["image_format"] == "webp"
