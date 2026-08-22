from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from helpers import PAYLOAD, FakeUpstream, write_test_config


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
        # 计费数据里有、但不在 app.novelai_enums.Model 白名单里的模型：能算出价格，
        # 所以会走到 free_small_only 策略而不是被计费层的未知模型拦截拦下。
        unknown_model_payload = PAYLOAD | {"model": "nai-diffusion-furry2"}
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
