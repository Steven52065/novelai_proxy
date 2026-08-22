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

def test_generate_validation_rejects_unknown_sampler_before_free_small_only(tmp_path: Path, monkeypatch):
    """未知采样器现在由 generate 参数校验先拦截（400 中文），不再走到 free_small_only（403）。"""
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

        assert resp.status_code == 400
        body = resp.json()
        assert "sampler" in body["message"]
        assert "future_sampler" in body["message"]

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
        assert upscale.json()["message"] == "用户无权访问接口：upscale"

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

def test_free_small_only_generate_rejection_has_chinese_message_and_reasons(tmp_path: Path, monkeypatch):
    """generate 被 free_small_only 拦截时返回中文总述 + 逐条中文 reasons。"""
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "free-only-reasons", "tier": "normal", "anlas_total": 100, "free_small_only": True},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        payload = PAYLOAD | {"parameters": PAYLOAD["parameters"] | {"steps": 29}}
        resp = client.post("/ai/generate-image", headers=headers, json=payload)

        assert resp.status_code == 403
        body = resp.json()
        assert "免费" in body["message"]
        assert body["reasons"]
        assert any("steps" in reason and "29" in reason for reason in body["reasons"])
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        rejected_log = next(row for row in logs if row["status"] == "rejected")
        assert rejected_log["error_code"] == "free_small_only_blocked"
        assert "免费" in rejected_log["error_message"]


def test_free_small_only_generate_reasons_cover_pixels_samples_unknown_and_forbidden_keys(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "free-only-reasons-more", "tier": "normal", "anlas_total": 100, "free_small_only": True},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        cases = [
            (
                {"width": 1216, "height": 1216},
                "图片像素超过免费上限",
            ),
            (
                {"n_samples": 2},
                "生成数量超过 1",
            ),
            (
                {"future_paid_parameter": True},
                "存在未知参数：future_paid_parameter",
            ),
            (
                {"image": "base64-image", "strength": 0.5},
                "参数 image 不允许用于免费小图",
            ),
        ]
        for overrides, expected_reason in cases:
            payload = PAYLOAD | {"parameters": PAYLOAD["parameters"] | overrides}
            resp = client.post("/ai/generate-image", headers=headers, json=payload)
            assert resp.status_code == 403, overrides
            body = resp.json()
            assert "免费" in body["message"], overrides
            assert any(expected_reason in reason for reason in body["reasons"]), (overrides, body)


def test_free_small_only_non_generate_endpoints_returns_chinese_message(tmp_path: Path, monkeypatch):
    """非 generate 端点（upscale 等）的 free_small_only 文案保持英文。"""
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "free-only-english",
                "tier": "normal",
                "anlas_total": 100,
                "free_small_only": True,
                "allowed_endpoints": ["generate-image", "upscale"],
            },
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        upscale = client.post(
            "/ai/upscale",
            headers=headers,
            json={"image": "aW1n", "width": 64, "height": 64, "scale": 2},
        )

        assert upscale.status_code == 403
        assert upscale.json()["message"] == "用户仅限免费小图生成，该请求不符合免费条件"
        assert "reasons" not in upscale.json()
