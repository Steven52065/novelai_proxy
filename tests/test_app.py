from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app.upstream import UpstreamClient


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
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w") as zip_file:
            zip_file.writestr("image.png", b"fake-image")
        return buffer.getvalue()

    async def generate_image_payload_zip(self, payload):
        return await self.generate_image_zip(payload)

    async def encode_vibe_binary(self, payload):
        return b"fake-vibe"

    async def upscale_zip(self, req):
        return b"fake-upscale-zip"

    async def augment_image_zip(self, req):
        return b"fake-augment-zip"

    async def suggest_tags(self, model: str, prompt: str, lang: str = "en"):
        return {"tags": []}


class FakeImageUpstream:
    async def generate_image_zip(self, req):
        image_buffer = io.BytesIO()
        Image.new("RGBA", (8, 8), (255, 0, 0, 128)).save(image_buffer, format="PNG")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w") as zip_file:
            zip_file.writestr("image.png", image_buffer.getvalue())
            zip_file.writestr("metadata.txt", b"keep-me")
        return buffer.getvalue()

    async def generate_image_payload_zip(self, payload):
        return await self.generate_image_zip(payload)


class FakeQueueSnapshot:
    def qsize(self):
        return 1

    def snapshot(self):
        return {
            "queue_size": 1,
            "running": {
                "request_id": "running-request",
                "user_id": 1,
                "action": "generate",
                "tier": "normal",
                "estimated_anlas_cost": 0,
                "priority": 10,
                "position": 0,
                "status": "running",
                "queued_seconds": 3,
                "running_seconds": 1,
            },
            "queued": [
                {
                    "request_id": "queued-request",
                    "user_id": 1,
                    "action": "generate",
                    "tier": "vip",
                    "estimated_anlas_cost": 5,
                    "priority": 0,
                    "position": 1,
                    "status": "queued",
                    "queued_seconds": 8,
                }
            ],
        }


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
logging:
  level: DEBUG
  directory: "{(tmp_path / "logs").as_posix()}"
cors:
  enabled: true
  allow_origins:
    - "https://client.example"
  allow_methods:
    - "*"
  allow_headers:
    - "*"
  expose_headers:
    - Content-Disposition
""",
        encoding="utf-8",
    )
    return config_path


def write_test_config_with_image_conversion(tmp_path: Path, image_format: str = "webp") -> Path:
    config_path = write_test_config(tmp_path)
    with config_path.open("a", encoding="utf-8") as f:
        f.write(
            f"""
image_conversion:
  enabled: true
  format: {image_format}
  quality: 80
"""
        )
    return config_path


class FakeBinaryResponse:
    status_code = 201
    content = b"zip-bytes"
    headers = {"Content-Type": "application/zip"}


class FakeBinarySession:
    headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, data):
        return FakeBinaryResponse()


class FakeBinaryCredential:
    async def get_session(self):
        return FakeBinarySession()


def test_upstream_accepts_official_application_zip_content_type(monkeypatch):
    client = UpstreamClient("token")
    monkeypatch.setattr(client, "_credential", lambda: FakeBinaryCredential())

    import asyncio

    assert asyncio.run(client._post_binary("https://image.novelai.net/ai/generate-image", {})) == b"zip-bytes"


def test_health_admin_create_user_and_subscription(tmp_path: Path, monkeypatch):
    config_path = write_test_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
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

        log_text = (tmp_path / "logs" / "novelai_proxy.log").read_text(encoding="utf-8")
        assert "http request completed method=GET path=/health status=200" in log_text
        assert "http request details method=GET path=/health" in log_text
    finally:
        monkeypatch.delenv("NOVELAI_PROXY_CONFIG", raising=False)


def test_generate_requires_valid_proxy_key(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        resp = client.post("/ai/generate-image", json=PAYLOAD)
        assert resp.status_code == 401
        assert resp.json()["message"] == "Invalid or missing API Key"


def test_validation_error_is_logged(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "invalid-payload", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post(
            "/ai/generate-image",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"bad": "payload"},
        )

        assert resp.status_code == 400

    log_text = (tmp_path / "logs" / "novelai_proxy.log").read_text(encoding="utf-8")
    assert "generate-image payload validation failed after normalization" in log_text


def test_generate_normalizes_official_payload_variants(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    payload = PAYLOAD | {
        "parameters": PAYLOAD["parameters"] | {
            "skip_cfg_above_sigma": 59.04722600415217,
            "seed": 6816488388,
        }
    }
    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "official-payload", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=payload)

        assert resp.status_code == 201
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        success_log = next(row for row in logs if row["status"] == "success")
        assert success_log["request_payload"]["parameters"]["skip_cfg_above_sigma"] == 60
        assert 0 < success_log["request_payload"]["parameters"]["seed"] <= 4294967288


def test_generate_preserves_reference_fields_and_charges_extra_anlas(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    payload = PAYLOAD | {
        "parameters": PAYLOAD["parameters"] | {
            "director_reference_images": ["ref-a", "ref-b"],
            "director_reference_strength_values": [0.6, 0.7],
            "director_reference_secondary_strength_values": [0.2, 0.3],
            "director_reference_information_extracted": [1, 1],
            "reference_image_multiple": ["v1", "v2", "v3", "v4", "v5"],
            "reference_strength_multiple": [0.5, 0.5, 0.5, 0.5, 0.5],
            "reference_information_extracted_multiple": [1, 1, 1, 1, 1],
        }
    }
    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "reference-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=payload)

        assert resp.status_code == 201
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        success_log = next(row for row in logs if row["status"] == "success")
        params = success_log["request_payload"]["parameters"]
        assert params["director_reference_images"] == ["ref-a", "ref-b"]
        assert params["reference_image_multiple"] == ["v1", "v2", "v3", "v4", "v5"]
        assert success_log["estimated_anlas_cost"] == 12


def test_encode_vibe_is_queued_and_charged(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "vibe-user", "tier": "normal", "anlas_total": 100},
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

        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        success_log = next(row for row in logs if row["status"] == "success")
        assert success_log["request_payload"]["input"] == "1girl"
        assert success_log["request_payload"]["parameters"]["sampler"] == "k_euler_ancestral"
        assert len(success_log["output_files"]) == 1
        assert Path(success_log["output_files"][0]).read_bytes() == b"fake-image"


def test_generate_can_convert_response_images_to_webp(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_image_conversion(tmp_path, "webp")))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeImageUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "webp-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

        assert resp.status_code == 201
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zip_file:
            assert "image.webp" in zip_file.namelist()
            assert "metadata.txt" in zip_file.namelist()
            with Image.open(io.BytesIO(zip_file.read("image.webp"))) as image:
                assert image.format == "WEBP"
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        success_log = next(row for row in logs if row["status"] == "success")
        assert success_log["output_files"][0].endswith(".webp")


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


def test_admin_queue_status_includes_live_items(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "queue-user", "tier": "vip", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        app.state.proxy_queue = FakeQueueSnapshot()
        now = "2026-05-27T00:00:00+00:00"
        app.state.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, log_level, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("running-request", user_id, "generate", "nai-diffusion-3", 512, 768, 1, 1, 0, "running", "INFO", now),
        )
        app.state.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, log_level, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("queued-request", user_id, "generate", "nai-diffusion-3", 1024, 1024, 28, 1, 5, "queued", "INFO", now),
        )

        resp = client.get("/admin/api/queue", auth=("admin", "admin123"))

        assert resp.status_code == 200
        body = resp.json()
        assert body["queue_size"] == 1
        assert body["running"]["user_name"] == "queue-user"
        assert body["running"]["width"] == 512
        assert body["queued"][0]["position"] == 1
        assert body["queued"][0]["estimated_anlas_cost"] == 5
