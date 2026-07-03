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
        assert success_log["estimated_anlas_cost"] == 12
