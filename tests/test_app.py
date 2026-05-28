from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

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
    def __init__(self):
        self.last_generate_payload = None

    async def generate_image_zip(self, req):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w") as zip_file:
            zip_file.writestr("image.png", b"fake-image")
        return buffer.getvalue()

    async def generate_image_payload_zip(self, payload):
        self.last_generate_payload = payload
        return await self.generate_image_zip(payload)

    async def encode_vibe_binary(self, payload):
        return b"fake-vibe"

    async def upscale_zip(self, req):
        return b"fake-upscale-zip"

    async def augment_image_zip(self, req):
        return b"fake-augment-zip"

    async def suggest_tags(self, model: str, prompt: str, lang: str = "en"):
        return {"tags": []}


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


def write_test_config_with_image_format_policy(tmp_path: Path, mode: str = "request", image_format: str = "webp") -> Path:
    config_path = write_test_config(tmp_path)
    with config_path.open("a", encoding="utf-8") as f:
        f.write(
            f"""
image_format:
  mode: {mode}
  format: {image_format}
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
    assert "generate-image payload validation failed errors=" in log_text


def test_generate_preserves_non_cost_official_payload_fields(tmp_path: Path, monkeypatch):
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
        assert success_log["request_payload"]["parameters"]["skip_cfg_above_sigma"] == 59.04722600415217
        assert success_log["request_payload"]["parameters"]["seed"] == 6816488388


def test_generate_passes_unknown_official_fields_without_sdk_validation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    payload = PAYLOAD | {
        "model": "future-official-model",
        "parameters": PAYLOAD["parameters"] | {
            "director_reference_descriptions": [{"caption": {"base_caption": "ref"}}],
            "prompt": "official parameter prompt",
            "stream": "sse",
        },
    }
    with TestClient(app) as client:
        fake_upstream = FakeUpstream()
        app.state.upstream = fake_upstream
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "future-model-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=payload)

        assert resp.status_code == 201
        assert fake_upstream.last_generate_payload["model"] == "future-official-model"
        params = fake_upstream.last_generate_payload["parameters"]
        assert params["director_reference_descriptions"] == [{"caption": {"base_caption": "ref"}}]
        assert params["prompt"] == "official parameter prompt"
        assert params["stream"] == "sse"


def test_generate_rejects_missing_cost_fields(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    payload = PAYLOAD | {"parameters": {"width": 832, "height": 1216, "steps": 23}}
    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "missing-cost-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=payload)

        assert resp.status_code == 400
        assert "n_samples" in resp.json()["details"]


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


def test_free_small_only_allows_definite_free_opus_small(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "free-only-user", "tier": "normal", "anlas_total": 0, "free_small_only": True},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

        assert resp.status_code == 201
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        success_log = next(row for row in logs if row["status"] == "success")
        assert success_log["estimated_anlas_cost"] == 0


def test_free_small_only_allows_known_transport_and_empty_cached_reference_fields(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    payload = PAYLOAD | {
        "model": "nai-diffusion-4-5-full",
        "parameters": PAYLOAD["parameters"] | {
            "width": 1024,
            "height": 1024,
            "steps": 28,
            "sampler": "k_euler",
            "inpaintImg2ImgStrength": 1,
            "normalize_reference_strength_multiple": False,
            "reference_image_multiple_cached": [],
            "stream": "msgpack",
        },
    }
    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "free-only-v4-fields", "tier": "normal", "anlas_total": 0, "free_small_only": True},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=payload)

        assert resp.status_code == 201
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        success_log = next(row for row in logs if row["status"] == "success")
        assert success_log["estimated_anlas_cost"] == 0
        assert success_log["request_payload"]["parameters"]["stream"] == "msgpack"


def test_free_small_only_rejects_nonempty_cached_reference_field(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    payload = PAYLOAD | {
        "parameters": PAYLOAD["parameters"] | {
            "reference_image_multiple_cached": ["cached-reference-id"],
        },
    }
    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "free-only-cached-ref", "tier": "normal", "anlas_total": 100, "free_small_only": True},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=payload)

        assert resp.status_code == 403
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        rejected_log = next(row for row in logs if row["status"] == "rejected")
        assert rejected_log["error_code"] == "free_small_only_blocked"


def test_free_small_only_rejects_paid_or_uncertain_request(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "free-only-blocked", "tier": "normal", "anlas_total": 100, "free_small_only": True},
        )
        api_key = create_resp.json()["api_key"]

        paid_payload = PAYLOAD | {"parameters": PAYLOAD["parameters"] | {"steps": 29}}
        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=paid_payload)

        assert resp.status_code == 403
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        rejected_log = next(row for row in logs if row["status"] == "rejected")
        assert rejected_log["error_code"] == "free_small_only_blocked"


def test_free_small_only_rejects_unknown_sampler_even_if_cost_is_zero(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "free-only-unknown", "tier": "normal", "anlas_total": 100, "free_small_only": True},
        )
        api_key = create_resp.json()["api_key"]

        payload = PAYLOAD | {"parameters": PAYLOAD["parameters"] | {"sampler": "future_sampler"}}
        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=payload)

        assert resp.status_code == 403


def test_free_small_only_rejects_img2img_unknown_model_and_unknown_parameters(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "free-only-strict", "tier": "normal", "anlas_total": 100, "free_small_only": True},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        img2img_payload = PAYLOAD | {"action": "img2img", "parameters": PAYLOAD["parameters"] | {"image": "aW1n"}}
        unknown_model_payload = PAYLOAD | {"model": "future-official-model"}
        unknown_parameter_payload = PAYLOAD | {"parameters": PAYLOAD["parameters"] | {"future_paid_parameter": True}}

        assert client.post("/ai/generate-image", headers=headers, json=img2img_payload).status_code == 403
        assert client.post("/ai/generate-image", headers=headers, json=unknown_model_payload).status_code == 403
        assert client.post("/ai/generate-image", headers=headers, json=unknown_parameter_payload).status_code == 403


def test_default_user_is_limited_to_generate_image_endpoint(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "endpoint-limited", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        generate = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        upscale = client.post(
            "/ai/upscale",
            headers=headers,
            json={"image": "aW1n", "width": 64, "height": 64, "scale": 2},
        )

        assert generate.status_code == 201
        assert upscale.status_code == 403
        assert upscale.json()["message"] == "User is not allowed to access endpoint: upscale"


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


def test_free_small_only_rejects_vibe_upscale_and_augment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "free-only-tools",
                "tier": "normal",
                "anlas_total": 100,
                "free_small_only": True,
                "allowed_endpoints": ["generate-image", "encode-vibe", "upscale", "augment-image"],
            },
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        encode_vibe = client.post(
            "/ai/encode-vibe",
            headers=headers,
            json={"image": "aW1n", "model": "nai-diffusion-4-5-full", "information_extracted": 1},
        )
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

        assert encode_vibe.status_code == 403
        assert upscale.status_code == 403
        assert augment.status_code == 403
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        rejected_logs = [row for row in logs if row["status"] == "rejected"]
        assert {row["action"] for row in rejected_logs} == {"encode-vibe", "upscale", "sketch"}
        assert {row["error_code"] for row in rejected_logs} == {"free_small_only_blocked"}


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


def test_admin_database_management_clears_large_payloads(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    old_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    recent_time = datetime.now(timezone.utc).isoformat()
    large_payload = '{"image":"' + ("a" * 2048) + '"}'

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "db-clean-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        for request_id, created_at in (("old-large-payload", old_time), ("recent-large-payload", recent_time)):
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level,
                    request_payload, output_files, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, user_id, "generate", 0, "success", "INFO", large_payload, '["image.png"]', created_at),
            )

        stats = client.get("/admin/api/database/stats", auth=("admin", "admin123"))
        assert stats.status_code == 200
        assert stats.json()["usage_logs"]["logs_with_payload"] == 2

        clear_resp = client.post(
            "/admin/api/database/clear-payloads",
            auth=("admin", "admin123"),
            json={"older_than_days": 7, "min_payload_kb": 1, "clear_output_files": False},
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json()["updated_logs"] == 1

        old_row = app.state.db.query_one(
            "SELECT request_payload, output_files FROM usage_logs WHERE request_id = ?",
            ("old-large-payload",),
        )
        recent_row = app.state.db.query_one(
            "SELECT request_payload, output_files FROM usage_logs WHERE request_id = ?",
            ("recent-large-payload",),
        )
        assert old_row["request_payload"] is None
        assert old_row["output_files"] == '["image.png"]'
        assert recent_row["request_payload"] == large_payload


def test_admin_database_management_deletes_old_logs_by_status(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    old_time = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    recent_time = datetime.now(timezone.utc).isoformat()

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "db-delete-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        rows = [
            ("old-success-log", "success", old_time),
            ("old-rejected-log", "rejected", old_time),
            ("recent-rejected-log", "rejected", recent_time),
        ]
        for request_id, status, created_at in rows:
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, user_id, "generate", 0, status, "INFO", created_at),
            )

        cleanup_resp = client.post(
            "/admin/api/database/cleanup-logs",
            auth=("admin", "admin123"),
            json={"older_than_days": 30, "statuses": ["rejected"]},
        )
        assert cleanup_resp.status_code == 200
        assert cleanup_resp.json()["deleted_logs"] == 1

        remaining_ids = {
            row["request_id"]
            for row in app.state.db.query_all("SELECT request_id FROM usage_logs ORDER BY request_id")
        }
        assert "old-rejected-log" not in remaining_ids
        assert {"old-success-log", "recent-rejected-log"} <= remaining_ids


def test_admin_database_page_and_vacuum(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        page = client.get("/admin/database")
        assert page.status_code == 200
        assert "数据库管理" in page.text
        assert "清空大 Payload" in page.text

        api_resp = client.post("/admin/api/database/vacuum", auth=("admin", "admin123"))
        assert api_resp.status_code == 200
        assert api_resp.json()["ok"] is True

        form_resp = client.post("/admin/database/vacuum", follow_redirects=False)
        assert form_resp.status_code == 303
        assert "/admin/database" in form_resp.headers["location"]


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
