from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from helpers import PAYLOAD, BlockingFakeUpstream, FakeQueueSnapshot, write_test_config, write_test_config_with_upstreams
from queue_manager_helpers import _queued_user_names, _wait_until


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

def test_queue_snapshot_orders_queued_items_by_actual_dispatch_sequence(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "NOVELAI_PROXY_CONFIG",
        str(write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"], max_queue_size=2)),
    )
    from app.main import app

    release_a = threading.Event()
    release_b = threading.Event()
    with TestClient(app) as client:
        upstream_a = BlockingFakeUpstream(release_a)
        upstream_b = BlockingFakeUpstream(release_b)
        app.state.upstream = upstream_a
        app.state.upstream_clients["opus-b"] = upstream_b

        user_a = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "queued-a", "tier": "normal", "anlas_total": 100, "allowed_upstreams": ["opus-a"]},
        ).json()["api_key"]
        user_b = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "queued-b", "tier": "normal", "anlas_total": 100, "allowed_upstreams": ["opus-b"]},
        ).json()["api_key"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            running_a = pool.submit(client.post, "/ai/generate-image", headers={"Authorization": f"Bearer {user_a}"}, json=PAYLOAD)
            _wait_until(lambda: len(upstream_a.generate_started_at) == 1)

            queued_a = pool.submit(client.post, "/ai/generate-image", headers={"Authorization": f"Bearer {user_a}"}, json=PAYLOAD)
            _wait_until(lambda: _queued_user_names(client) == ["queued-a"])

            running_b = pool.submit(client.post, "/ai/generate-image", headers={"Authorization": f"Bearer {user_b}"}, json=PAYLOAD)
            _wait_until(lambda: len(upstream_b.generate_started_at) == 1)

            queued_b = pool.submit(client.post, "/ai/generate-image", headers={"Authorization": f"Bearer {user_b}"}, json=PAYLOAD)
            _wait_until(lambda: _queued_user_names(client) == ["queued-a", "queued-b"])

            queue = client.get("/admin/api/queue", auth=("admin", "admin123")).json()
            queued_names = [item["user_name"] for item in queue["queued"]]
            queued_positions = [item["position"] for item in queue["queued"]]

            assert queued_names == ["queued-a", "queued-b"]
            assert queued_positions == [1, 2]
            assert all("sequence" not in item for item in queue["queued"])

            release_a.set()
            release_b.set()
            assert running_a.result(timeout=3).status_code == 201
            assert queued_a.result(timeout=3).status_code == 201
            assert running_b.result(timeout=3).status_code == 201
            assert queued_b.result(timeout=3).status_code == 201
