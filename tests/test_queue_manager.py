from __future__ import annotations

import concurrent.futures
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from helpers import (
    PAYLOAD,
    BlockingFakeUpstream,
    FailingThenSuccessfulUpstream,
    FakeQueueSnapshot,
    FakeUpstream,
    write_test_config,
    write_test_config_with_upstreams,
)


def test_queue_waits_between_upstream_requests(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path, upstream_interval_min_seconds=0.05)))
    from app.main import app

    with TestClient(app) as client:
        fake_upstream = FakeUpstream()
        app.state.upstream = fake_upstream
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "delay-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        first = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        second = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)

        assert first.status_code == 201
        assert second.status_code == 201
        assert len(fake_upstream.generate_started_at) == 2
        assert fake_upstream.generate_started_at[1] - fake_upstream.generate_started_at[0] >= 0.045

def test_queue_adds_extra_delay_after_upstream_api_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "NOVELAI_PROXY_CONFIG",
        str(
            write_test_config(
                tmp_path,
                upstream_interval_min_seconds=0.02,
                upstream_error_extra_delay_seconds=0.05,
            )
        ),
    )
    from app.main import app

    with TestClient(app) as client:
        fake_upstream = FailingThenSuccessfulUpstream()
        app.state.upstream = fake_upstream
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "upstream-error-delay-user", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        first = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        second = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)

        assert first.status_code == 429
        assert second.status_code == 201
        assert len(fake_upstream.generate_started_at) == 2
        assert fake_upstream.generate_started_at[1] - fake_upstream.generate_started_at[0] >= 0.065

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


def test_multi_upstream_round_robin_routes_requests(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"])))
    from app.main import app

    with TestClient(app) as client:
        upstream_a = FakeUpstream()
        upstream_b = FakeUpstream()
        app.state.upstream = upstream_a
        app.state.upstream_clients["opus-b"] = upstream_b
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "rr-user", "tier": "normal", "anlas_total": 100},
        )
        headers = {"Authorization": f"Bearer {create_resp.json()['api_key']}"}

        first = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        second = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        third = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)

        assert [first.status_code, second.status_code, third.status_code] == [201, 201, 201]
        assert len(upstream_a.generate_started_at) == 2
        assert len(upstream_b.generate_started_at) == 1
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        success_upstreams = [row["upstream_id"] for row in logs if row["status"] == "success"]
        assert sorted(success_upstreams) == ["opus-a", "opus-a", "opus-b"]


def test_user_allowed_upstreams_limits_routing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"])))
    from app.main import app

    with TestClient(app) as client:
        upstream_a = FakeUpstream()
        upstream_b = FakeUpstream()
        app.state.upstream = upstream_a
        app.state.upstream_clients["opus-b"] = upstream_b
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={
                "name": "restricted-user",
                "tier": "normal",
                "anlas_total": 100,
                "allowed_upstreams": ["opus-b"],
            },
        )
        headers = {"Authorization": f"Bearer {create_resp.json()['api_key']}"}

        first = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        second = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)

        assert first.status_code == 201
        assert second.status_code == 201
        assert len(upstream_a.generate_started_at) == 0
        assert len(upstream_b.generate_started_at) == 2
        users = client.get("/admin/api/users", auth=("admin", "admin123")).json()["users"]
        created = next(row for row in users if row["name"] == "restricted-user")
        assert created["allowed_upstreams"] == "opus-b"
        assert created["allowed_upstreams_list"] == ["opus-b"]


def test_routing_tries_other_allowed_upstream_when_selected_queue_is_full(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "NOVELAI_PROXY_CONFIG",
        str(write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"], max_queue_size=1)),
    )
    from app.main import app

    release_a = threading.Event()
    with TestClient(app) as client:
        upstream_a = BlockingFakeUpstream(release_a)
        upstream_b = FakeUpstream()
        app.state.upstream = upstream_a
        app.state.upstream_clients["opus-b"] = upstream_b
        fill_user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "fill-a", "tier": "normal", "anlas_total": 100, "allowed_upstreams": ["opus-a"]},
        ).json()["api_key"]
        flexible_user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "flexible", "tier": "normal", "anlas_total": 100},
        ).json()["api_key"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(
                client.post,
                "/ai/generate-image",
                headers={"Authorization": f"Bearer {fill_user}"},
                json=PAYLOAD,
            )
            deadline = time.monotonic() + 3
            while len(upstream_a.generate_started_at) < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            queued = pool.submit(
                client.post,
                "/ai/generate-image",
                headers={"Authorization": f"Bearer {fill_user}"},
                json=PAYLOAD,
            )
            time.sleep(0.05)

            flexible = client.post(
                "/ai/generate-image",
                headers={"Authorization": f"Bearer {flexible_user}"},
                json=PAYLOAD,
            )

            assert flexible.status_code == 201
            assert len(upstream_b.generate_started_at) == 1
            release_a.set()
            assert running.result(timeout=3).status_code == 201
            assert queued.result(timeout=3).status_code == 201
