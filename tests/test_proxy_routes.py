from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from fastapi.testclient import TestClient
from novelai_python._exceptions import APIError

from helpers import (
    PAYLOAD,
    FakeImageHosting,
    FakeUpstream,
    write_test_config,
    write_test_config_with_upstreams,
    write_test_config_with_image_format_policy,
    _wait_for_log_image_urls,
)


class UpstreamApiErrorFake(FakeUpstream):
    async def generate_image_payload_zip(self, payload):
        raise APIError(
            "secret upstream detail",
            request=payload,
            response={"message": "secret upstream response", "token": "secret-token"},
            code="418",
        )

    async def suggest_tags(self, model: str, prompt: str, lang: str = "en"):
        raise APIError(
            "secret suggest detail",
            request={"model": model, "prompt": prompt, "lang": lang},
            response={"message": "secret suggest response", "token": "secret-token"},
            code="429",
        )


class InternalErrorFake(FakeUpstream):
    async def generate_image_payload_zip(self, payload):
        raise RuntimeError("secret internal detail")

    async def suggest_tags(self, model: str, prompt: str, lang: str = "en"):
        raise RuntimeError("secret suggest internal detail")


class NeverReturningUpstream(FakeUpstream):
    async def generate_image_payload_zip(self, payload):
        self.generate_started_at.append(0)
        self.last_generate_payload = payload
        await asyncio.Event().wait()


class Always429Upstream(FakeUpstream):
    async def generate_image_payload_zip(self, payload):
        self.generate_started_at.append(threading.get_native_id())
        raise APIError(
            "Too many requests",
            request=payload,
            response={"message": "Too many requests"},
            code="429",
        )


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

def test_generate_malformed_json_returns_400(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "bad-json-generate", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post(
            "/ai/generate-image",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            content="{",
        )

        assert resp.status_code == 400
        assert resp.json() == {"message": "Invalid request"}

    log_text = (tmp_path / "logs" / "novelai_proxy.log").read_text(encoding="utf-8")
    assert "generate-image JSON parsing failed errors=" in log_text

def test_encode_vibe_malformed_json_returns_400(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "bad-json-vibe",
                "tier": "normal",
                "anlas_total": 100,
                "allowed_endpoints": ["generate-image", "encode-vibe"],
            },
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post(
            "/ai/encode-vibe",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            content="{",
        )

        assert resp.status_code == 400
        assert resp.json() == {"message": "Invalid request"}

    log_text = (tmp_path / "logs" / "novelai_proxy.log").read_text(encoding="utf-8")
    assert "encode-vibe JSON parsing failed errors=" in log_text

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


def test_generate_records_duration_metrics_in_usage_log_and_page(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "duration-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

        assert resp.status_code == 201
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        success_log = next(row for row in logs if row["status"] == "success")
        assert isinstance(success_log["total_ms"], int)
        assert isinstance(success_log["upstream_ms"], int)
        assert success_log["total_ms"] >= 0
        assert success_log["upstream_ms"] >= 0
        assert success_log["total_ms_display"] != "-"
        assert success_log["upstream_ms_display"] != "-"

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        page = client.get("/admin/logs")
        assert page.status_code == 200
        assert "总时长" in page.text
        assert "上游时长" in page.text
        assert success_log["total_ms_display"] in page.text
        assert success_log["upstream_ms_display"] in page.text

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
        assert resp.json() == {"message": "Invalid request"}


def test_generate_upstream_api_error_returns_generic_message(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = UpstreamApiErrorFake()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "upstream-error-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

        assert resp.status_code == 418
        assert resp.json() == {"message": "Upstream request failed"}
        assert "secret" not in resp.text


def test_generate_internal_error_returns_generic_message(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = InternalErrorFake()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "internal-error-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

        assert resp.status_code == 502
        assert resp.json() == {"message": "Proxy request failed"}
        assert "secret" not in resp.text


def test_generate_upstream_execution_timeout_returns_504_and_releases_quota(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "NOVELAI_PROXY_CONFIG",
        str(write_test_config(tmp_path, upstream_execution_timeout_seconds=0.02)),
    )
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = NeverReturningUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "timeout-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        paid_payload = PAYLOAD | {"parameters": PAYLOAD["parameters"] | {"steps": 29}}

        resp = client.post("/ai/generate-image", headers=headers, json=paid_payload)

        assert resp.status_code == 504
        assert resp.json() == {"message": "Upstream request timed out"}
        quota = client.get("/user/subscription", headers=headers).json()["proxyQuota"]
        assert quota["used"] == 0
        assert quota["reserved"] == 0
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        failed_log = next(row for row in logs if row["status"] == "failed")
        assert failed_log["error_code"] == "upstream_timeout"
        assert "0.02" in failed_log["error_message"]


def test_suggest_tags_upstream_api_error_returns_generic_message(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = UpstreamApiErrorFake()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "suggest-upstream-error-user",
                "tier": "normal",
                "anlas_total": 100,
                "allowed_endpoints": ["generate-image", "suggest-tags"],
            },
        )
        api_key = create_resp.json()["api_key"]

        resp = client.get(
            "/ai/generate-image/suggest-tags",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"model": "nai-diffusion-3", "prompt": "1girl"},
        )

        assert resp.status_code == 429
        assert resp.json() == {"message": "Upstream request failed"}
        assert "secret" not in resp.text


def test_suggest_tags_internal_error_returns_generic_message(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = InternalErrorFake()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "suggest-internal-error-user",
                "tier": "normal",
                "anlas_total": 100,
                "allowed_endpoints": ["generate-image", "suggest-tags"],
            },
        )
        api_key = create_resp.json()["api_key"]

        resp = client.get(
            "/ai/generate-image/suggest-tags",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"model": "nai-diffusion-3", "prompt": "1girl"},
        )

        assert resp.status_code == 503
        assert resp.json() == {"message": "Suggest tags request failed"}
        assert "secret" not in resp.text

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
        assert success_log["image_urls"] == []


def test_rate_limit_counts_retry_attempts_as_one_user_request(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"])))
    from app.main import app

    with TestClient(app) as client:
        upstream_a = Always429Upstream()
        upstream_b = FakeUpstream()
        app.state.upstream = upstream_a
        app.state.upstream_clients["opus-b"] = upstream_b
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "retry-rate-limit", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        client.post(
            f"/admin/api/users/{user_id}/rate-limit-rules",
            auth=("admin", "admin123"),
            json={"period": "minute", "max_requests": 2},
        )

        retried = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        second = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        third = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)

        assert retried.status_code == 201
        assert second.status_code == 201
        assert third.status_code == 429

        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        retry_success_log = next(row for row in logs if row["status"] == "success" and row["is_retry_success"] == 1)
        retried_rows = [row for row in logs if row["request_id"] == retry_success_log["request_id"]]
        retry_failed_log = next(row for row in logs if row["request_id"] == retry_success_log["request_id"] and row["status"] == "failed")
        assert retry_success_log["upstream_id"] == "opus-b"
        assert retry_failed_log["upstream_id"] == "opus-a"
        assert len(retried_rows) == 2


def test_429_retry_can_reuse_same_upstream_until_max_attempts(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "NOVELAI_PROXY_CONFIG",
        str(write_test_config_with_upstreams(tmp_path, ["opus-a"], retry_429_max_attempts=3)),
    )
    from app.main import app

    with TestClient(app) as client:
        upstream_a = Always429Upstream()
        app.state.upstream = upstream_a
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "single-upstream-retry", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

        assert resp.status_code == 429
        assert len(upstream_a.generate_started_at) == 3
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        failed_rows = [row for row in logs if row["status"] == "failed"]
        assert [row["attempt_number"] for row in sorted(failed_rows, key=lambda row: row["attempt_number"])] == [0, 1, 2]
        assert {row["upstream_id"] for row in failed_rows} == {"opus-a"}


def test_generate_uploads_images_to_configured_image_host(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        release_upload = threading.Event()
        fake_hosting = FakeImageHosting(release_upload)
        app.state.upstream = FakeUpstream()
        app.state.proxy_queue.image_hosting = fake_hosting
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "image-host-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

        assert resp.status_code == 201
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        success_log = next(row for row in logs if row["status"] == "success")
        assert fake_hosting.uploaded_request_ids == [success_log["request_id"]]
        assert success_log["image_urls"] == []

        release_upload.set()
        success_log = _wait_for_log_image_urls(client, success_log["request_id"])
        assert success_log["image_urls"] == [
            {
                "provider": "catbox",
                "url": "https://files.catbox.moe/fake-image.png",
                "filename": "image.png",
                "bytes": 10,
                "index": 1,
            }
        ]

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        page = client.get("/admin/logs")
        assert page.status_code == 200
        assert "https://files.catbox.moe/fake-image.png" in page.text
        assert "图床图片" in page.text

def test_generate_skips_image_host_upload_when_pending_limit_reached(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        release_upload = threading.Event()
        fake_hosting = FakeImageHosting(release_upload, max_pending_uploads=1)
        app.state.upstream = FakeUpstream()
        app.state.proxy_queue.image_hosting = fake_hosting
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "image-host-limit-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        try:
            first = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)
            second = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

            assert first.status_code == 201
            assert second.status_code == 201
            assert len(fake_hosting.uploaded_request_ids) == 1
        finally:
            release_upload.set()

        _wait_for_log_image_urls(client, fake_hosting.uploaded_request_ids[0])
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        skipped_log = next(row for row in logs if row["request_id"] not in fake_hosting.uploaded_request_ids)
        assert skipped_log["status"] == "success"
        assert skipped_log["image_urls"] == []

def test_image_host_upload_pending_limit_zero_allows_unlimited_tasks(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        release_upload = threading.Event()
        fake_hosting = FakeImageHosting(release_upload, max_pending_uploads=0)
        app.state.upstream = FakeUpstream()
        app.state.proxy_queue.image_hosting = fake_hosting
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "image-host-unlimited-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        try:
            first = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)
            second = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

            assert first.status_code == 201
            assert second.status_code == 201
            assert len(fake_hosting.uploaded_request_ids) == 2
        finally:
            release_upload.set()

        for request_id in fake_hosting.uploaded_request_ids:
            success_log = _wait_for_log_image_urls(client, request_id)
            assert success_log["status"] == "success"

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
