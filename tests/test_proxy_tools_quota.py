from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from helpers import PAYLOAD, FakeUpstream, write_test_config


def test_encode_vibe_is_queued_and_charged(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "vibe-user", "tier": "normal", "anlas_total": 100, "allowed_endpoints": ["generate-image", "encode-vibe"]},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post(
            "/ai/encode-vibe",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"image": "aW1n", "model": "nai-diffusion-4-5-full", "information_extracted": 1},
        )

        assert resp.status_code == 201
        assert resp.content == b"fake-vibe"
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        success_log = next(row for row in logs if row["status"] == "success")
        assert success_log["action"] == "encode-vibe"
        assert success_log["estimated_anlas_cost"] == 2
        assert success_log["output_files"] == []

def test_cors_preflight_uses_configured_origin(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        resp = client.options(
            "/ai/generate-image",
            headers={
                "Origin": "https://client.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "https://client.example"
        assert "authorization" in resp.headers["access-control-allow-headers"].lower()

def test_insufficient_quota_returns_402_for_paid_request(tmp_path: Path, monkeypatch):
    config_path = write_test_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    paid_payload = PAYLOAD | {
        "parameters": PAYLOAD["parameters"] | {"width": 1024, "height": 1024, "steps": 50}
    }
    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "low-quota", "tier": "normal", "anlas_total": 1},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=paid_payload)

        assert resp.status_code == 402
        assert resp.json()["message"].startswith("Insufficient anlas")

def test_upscale_and_augment_are_queued_and_logged(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "carol", "tier": "vip", "anlas_total": 100, "allowed_endpoints": ["generate-image", "upscale", "augment-image"]},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        upscale = client.post(
            "/ai/upscale",
            headers=headers,
            json={"image": "aW1n", "width": 64, "height": 64, "scale": 2},
        )
        augment = client.post(
            "/ai/augment-image",
            headers=headers,
            json={"req_type": "sketch", "width": 64, "height": 64, "image": "aW1n", "defry": 0},
        )
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]

        assert upscale.status_code == 201
        assert augment.status_code == 201
        assert {row["action"] for row in logs} >= {"upscale", "sketch"}
