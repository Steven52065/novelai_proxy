from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient


PAYLOAD = {
    "input": "1girl",
    "model": "nai-diffusion-3",
    "action": "generate",
    "parameters": {
        "width": 832,
        "height": 1216,
        "scale": 5.0,
        "sampler": "k_euler_ancestral",
        "steps": 23,
        "n_samples": 1,
        "ucPreset": 0,
        "qualityToggle": False,
        "sm": False,
        "sm_dyn": False,
    },
}


class FakeUpstream:
    async def generate_image_zip(self, req):
        return b"fake-zip"

    async def upscale_zip(self, req):
        return b"fake-upscale-zip"

    async def augment_image_zip(self, req):
        return b"fake-augment-zip"

    async def suggest_tags(self, model: str, prompt: str, lang: str = "en"):
        return {"tags": []}


def write_test_config(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
admin:
  username: admin
  password: admin123
server:
  host: 127.0.0.1
  port: 8080
queue:
  max_concurrent_upstream: 1
  max_queue_size: 2
novelai:
  api_key: ""
  account_tier: 3
database:
  path: "{db_path.as_posix()}"
""",
        encoding="utf-8",
    )
    return config_path


def test_health_admin_create_user_and_subscription(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    try:
        from app.main import app

        with TestClient(app) as client:
            assert client.get("/health").json()["status"] == "ok"

            create_resp = client.post(
                "/admin/api/users",
                auth=("admin", "admin123"),
                json={"name": "alice", "tier": "normal", "anlas_total": 100},
            )
            assert create_resp.status_code == 200
            api_key = create_resp.json()["api_key"]
            assert api_key.startswith("nai_proxy_")

            sub_resp = client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"})
            assert sub_resp.status_code == 200
            body = sub_resp.json()
            assert body["proxyQuota"]["total"] == 100
            assert body["proxyQuota"]["available"] == 100
    finally:
        monkeypatch.delenv("NOVELAI_PROXY_CONFIG", raising=False)


def test_generate_requires_valid_proxy_key(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        resp = client.post("/ai/generate-image", json=PAYLOAD)
        assert resp.status_code == 401
        assert resp.json()["message"] == "Invalid or missing API Key"


def test_rate_limit_returns_429(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "bob", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        api_key = create_resp.json()["api_key"]
        client.post(
            f"/admin/api/users/{user_id}/rate-limit-rules",
            auth=("admin", "admin123"),
            json={"period": "minute", "max_requests": 1},
        )

        first = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)
        second = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

        assert first.status_code == 201
        assert second.status_code == 429
        assert second.headers["retry-after"] == "60"


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
            json={"name": "carol", "tier": "vip", "anlas_total": 100},
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


def test_admin_login_page(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"}, follow_redirects=False)
        assert login.status_code == 303
        assert "novelai_proxy_admin" in login.headers["set-cookie"]

        dashboard = client.get("/admin")
        assert dashboard.status_code == 200
        assert "仪表盘" in dashboard.text
