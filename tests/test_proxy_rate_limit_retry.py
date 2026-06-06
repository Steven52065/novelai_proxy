from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from helpers import PAYLOAD, FakeUpstream, write_test_config, write_test_config_with_upstreams
from proxy_route_fakes import Always429Upstream


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
