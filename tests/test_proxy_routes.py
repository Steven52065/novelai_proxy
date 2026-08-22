from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from helpers import PAYLOAD, FakeUpstream, write_test_config
from proxy_route_fakes import InternalErrorFake, NeverReturningUpstream, UpstreamApiErrorFake


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
        "model": "nai-diffusion-4-5-full",
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
        assert fake_upstream.last_generate_payload["model"] == "nai-diffusion-4-5-full"
        params = fake_upstream.last_generate_payload["parameters"]
        assert params["director_reference_descriptions"] == [{"caption": {"base_caption": "ref"}}]
        assert params["prompt"] == "official parameter prompt"
        assert params["stream"] == "sse"

def test_generate_rejects_models_missing_from_the_pricing_data(tmp_path: Path, monkeypatch):
    """计费数据里没有的模型必须直接拦下，不能按最便宜的家族静默计价。

    anlas_pricing.model_family 对未知模型回退到 stableDiffusion，NovelAI 新发模型
    在同步之前会被少收约 30%。
    """
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        fake_upstream = FakeUpstream()
        app.state.upstream = fake_upstream
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "unknown-model-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        resp = client.post(
            "/ai/generate-image",
            headers=headers,
            json=PAYLOAD | {"model": "future-official-model"},
        )

        assert resp.status_code == 400
        assert resp.json() == {"message": "Unsupported model: future-official-model"}
        assert fake_upstream.last_generate_payload is None

        allowed = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        assert allowed.status_code == 201

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


def test_generate_settings_load_error_returns_500(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "settings-load-error-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        def fail_settings():
            raise RuntimeError("secret settings database detail")

        monkeypatch.setattr(app.state.upstream_runtime, "get_settings", fail_settings)

        resp = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=PAYLOAD)

        assert resp.status_code == 500
        assert resp.json() == {"message": "Failed to load NovelAI settings"}
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

        assert resp.status_code == 502
        assert resp.json() == {"message": "Proxy request failed"}
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
        # New frontend-compatible total: 17 base generation + 10 precise
        # reference cost (2 refs × 5) + 2 extra vibe-reference surcharge.
        assert success_log["estimated_anlas_cost"] == 29

def test_generate_rejects_invalid_sampler_with_chinese_400(tmp_path: Path, monkeypatch):
    """generate 入口校验 sampler 取值，枚举外的值返回 400 中文（含参数名/值/允许列表）。"""
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "validation-sampler", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        payload = PAYLOAD | {"parameters": PAYLOAD["parameters"] | {"sampler": "future_sampler"}}
        resp = client.post("/ai/generate-image", headers=headers, json=payload)

        assert resp.status_code == 400
        body = resp.json()
        assert "sampler" in body["message"]
        assert "future_sampler" in body["message"]
        assert app.state.upstream.last_generate_payload is None


def test_generate_non_string_sampler_returns_400_not_500(tmp_path: Path, monkeypatch):
    """回归：不可哈希的 sampler 曾在校验里抛 TypeError，导致 500。"""
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "validation-sampler-type", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        for bad_sampler in (["k_euler"], {"a": 1}):
            payload = PAYLOAD | {"parameters": PAYLOAD["parameters"] | {"sampler": bad_sampler}}
            resp = client.post("/ai/generate-image", headers=headers, json=payload)

            assert resp.status_code == 400, bad_sampler
            assert "sampler" in resp.json()["message"], bad_sampler
        assert app.state.upstream.last_generate_payload is None


def test_generate_rejects_invalid_noise_schedule_with_chinese_400(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "validation-noise", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        payload = PAYLOAD | {"parameters": PAYLOAD["parameters"] | {"noise_schedule": "future_schedule"}}
        resp = client.post("/ai/generate-image", headers=headers, json=payload)

        assert resp.status_code == 400
        assert "noise_schedule" in resp.json()["message"]
        assert app.state.upstream.last_generate_payload is None


def test_generate_passes_through_menu_incompatible_combinations(tmp_path: Path, monkeypatch):
    """前端下拉菜单不提供、但上游实测接受的组合必须放行。

    以下三组都已对真实上游验证返回 200 与正常图片，按前端家族表/噪点表拦截会误杀。
    """
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "validation-passthrough", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        cases = [
            ("nai-diffusion-3", {"sampler": "k_dpm_2"}),                        # 家族表外的采样器
            ("nai-diffusion-3", {"sampler": "k_dpmpp_3m_sde"}),                 # 不在任何家族表里
            ("nai-diffusion-3", {"sampler": "ddim_v3", "noise_schedule": "native"}),
        ]
        for model, overrides in cases:
            payload = PAYLOAD | {"model": model, "parameters": PAYLOAD["parameters"] | overrides}
            resp = client.post("/ai/generate-image", headers=headers, json=payload)

            assert resp.status_code == 201, (model, overrides, resp.text[:200])
            sent = app.state.upstream.last_generate_payload["parameters"]
            for key, value in overrides.items():
                assert sent[key] == value, (model, overrides)


def test_generate_valid_sampler_and_noise_schedule_still_201(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "validation-ok", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        payload = PAYLOAD | {
            "parameters": PAYLOAD["parameters"] | {"noise_schedule": "karras"},
        }
        resp = client.post("/ai/generate-image", headers=headers, json=payload)

        assert resp.status_code == 201
        assert app.state.upstream.last_generate_payload["parameters"]["noise_schedule"] == "karras"


def test_generate_validation_not_applied_to_img2img_and_infill(tmp_path: Path, monkeypatch):
    """img2img / infill 不启用 sampler / noise_schedule 硬校验。"""
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "validation-img2img", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        # future_sampler 在 generate 下会被校验拒绝，但 img2img / infill 不校验。
        img2img_payload = PAYLOAD | {
            "action": "img2img",
            "parameters": PAYLOAD["parameters"] | {
                "sampler": "future_sampler",
                "image": "base64-image",
                "strength": 0.5,
            },
        }
        infill_payload = PAYLOAD | {
            "model": "nai-diffusion-3-inpainting",
            "action": "infill",
            "parameters": PAYLOAD["parameters"] | {
                "sampler": "future_sampler",
                "image": "base64-image",
                "mask": "base64-mask",
                "strength": 0.5,
            },
        }

        img2img_resp = client.post("/ai/generate-image", headers=headers, json=img2img_payload)
        assert img2img_resp.status_code == 201
        assert "参数 sampler" not in img2img_resp.text

        infill_resp = client.post("/ai/generate-image", headers=headers, json=infill_payload)
        assert infill_resp.status_code == 201
        assert "参数 sampler" not in infill_resp.text
