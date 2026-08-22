from __future__ import annotations

import base64
import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.api_errors import DataSerializationError
from helpers import PAYLOAD, FakeUpstream, write_test_config


class InvalidToolZipUpstream(FakeUpstream):
    async def upscale_zip(self, req):
        raise DataSerializationError(
            "Invalid ZIP file received from the API.",
            request=req.model_dump(mode="json", exclude_none=True),
            response={},
            code="201",
        )


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
        assert resp.json()["message"].startswith("anlas 额度不足")

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
        by_action = {row["action"]: row for row in logs if row["status"] == "success"}
        assert by_action["upscale"]["request_payload"] == {
            "image": "aW1n",
            "width": 64,
            "height": 64,
            "scale": 2.0,
        }
        assert by_action["sketch"]["request_payload"] == {
            "req_type": "sketch",
            "width": 64,
            "height": 64,
            "image": "aW1n",
            "defry": 0,
        }


def test_bg_removal_disables_opus_free_sample_and_charges_expected_cost(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    expected_costs = {
        (512, 512): 20,
        (512, 768): 29,
        (832, 1216): 65,
        (1024, 1024): 65,
    }
    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "bg-removal-opus",
                "tier": "vip",
                "anlas_total": sum(expected_costs.values()),
                "allowed_endpoints": ["augment-image"],
            },
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        for width, height in expected_costs:
            response = client.post(
                "/ai/augment-image",
                headers=headers,
                json={
                    "req_type": "bg-removal",
                    "width": width,
                    "height": height,
                    "image": "aW1n",
                },
            )
            assert response.status_code == 201

        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        bg_removal_logs = {
            (row["width"], row["height"]): row
            for row in logs
            if row["action"] == "bg-removal" and row["status"] == "success"
        }
        assert {
            dimensions: row["estimated_anlas_cost"]
            for dimensions, row in bg_removal_logs.items()
        } == expected_costs
        assert all(row["final_anlas_cost"] == expected_costs[dimensions] for dimensions, row in bg_removal_logs.items())

        quota = client.get("/user/subscription", headers=headers).json()["proxyQuota"]
        assert quota["used"] == sum(expected_costs.values())
        assert quota["available"] == 0


def test_invalid_tool_zip_returns_gateway_error_and_releases_quota(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = InvalidToolZipUpstream()
        user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "invalid-tool-zip",
                "tier": "vip",
                "anlas_total": 100,
                "allowed_endpoints": ["upscale"],
            },
        ).json()
        headers = {"Authorization": f"Bearer {user['api_key']}"}

        response = client.post(
            "/ai/upscale",
            headers=headers,
            json={"image": "aW1n", "width": 64, "height": 64, "scale": 2},
        )

        assert response.status_code == 502
        assert response.json() == {"message": "上游请求失败"}
        quota = client.get("/user/subscription", headers=headers).json()["proxyQuota"]
        assert quota["used"] == 0
        assert quota["reserved"] == 0
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        failed_log = next(row for row in logs if row["action"] == "upscale")
        assert failed_log["status"] == "failed"
        assert failed_log["error_code"] == "201"
        assert failed_log["final_anlas_cost"] is None


def test_upscale_and_augment_models_preserve_request_validation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "validation-user",
                "tier": "vip",
                "anlas_total": 100,
                "allowed_endpoints": ["upscale", "augment-image"],
            },
        ).json()
        headers = {"Authorization": f"Bearer {user['api_key']}"}

        bad_upscale = client.post(
            "/ai/upscale",
            headers=headers,
            json={"image": "data:image/png;base64,aW1n", "width": 64, "height": 64},
        )
        missing_dimensions = client.post(
            "/ai/upscale",
            headers=headers,
            json={"image": "aW1n"},
        )
        bad_emotion = client.post(
            "/ai/augment-image",
            headers=headers,
            json={
                "req_type": "emotion",
                "width": 64,
                "height": 64,
                "image": "aW1n",
                "prompt": "smile",
            },
        )

        assert bad_upscale.status_code == 400
        assert missing_dimensions.status_code == 400
        assert bad_emotion.status_code == 400
        assert bad_upscale.json()["message"] == "无效的请求"
        assert isinstance(bad_upscale.json()["details"], list)


def test_validation_errors_do_not_echo_the_whole_image(tmp_path: Path, monkeypatch):
    """校验失败不能把整张 base64 图片写回响应体和错误日志。

    pydantic 会把出错字段的原始输入放进 errors()["input"]，未截断时一次失败就产生
    一份与请求等大的响应和一条等大的 ERROR 日志。
    """
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    image = base64.b64encode(os.urandom(120_000)).decode()
    image_tail = image[-64:]

    with TestClient(app) as client:
        user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "truncation-user",
                "tier": "vip",
                "anlas_total": 100,
                "allowed_endpoints": ["upscale", "augment-image"],
            },
        ).json()
        headers = {"Authorization": f"Bearer {user['api_key']}"}

        rejected = client.post(
            "/ai/augment-image",
            headers=headers,
            json={
                "req_type": "emotion",
                "width": 64,
                "height": 64,
                "image": image,
                "prompt": "smile",
            },
        )

        assert rejected.status_code == 400
        assert image_tail not in rejected.text
        assert len(rejected.content) < 4000

        echoed = next(entry["input"] for entry in rejected.json()["details"] if "input" in entry)
        echoed_image = echoed["image"] if isinstance(echoed, dict) else echoed
        assert len(echoed_image) < 400
        assert echoed_image.startswith(image[:100])
        assert "truncated" in echoed_image
