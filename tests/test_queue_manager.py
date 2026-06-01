from __future__ import annotations

import asyncio
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
from app.queue_manager import QueueFull, RoutingProxyQueue, UpstreamQueueTarget


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


def test_suggest_tags_does_not_advance_round_robin_routing(tmp_path: Path, monkeypatch):
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
                "name": "suggest-user",
                "tier": "normal",
                "anlas_total": 100,
                "allowed_endpoints": ["generate-image", "suggest-tags"],
            },
        )
        headers = {"Authorization": f"Bearer {create_resp.json()['api_key']}"}

        suggest = client.get(
            "/ai/generate-image/suggest-tags",
            headers=headers,
            params={"model": "nai-diffusion-3", "prompt": "1girl"},
        )
        generated = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)

        assert suggest.status_code == 200
        assert generated.status_code == 201
        assert len(upstream_a.suggest_tags_calls) == 1
        assert len(upstream_a.generate_started_at) == 1
        assert len(upstream_b.generate_started_at) == 0


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


def test_dispatch_queue_default_size_is_sum_of_upstream_queue_sizes():
    queue = RoutingProxyQueue(
        targets=[
            UpstreamQueueTarget(id="opus-a", client_provider=FakeUpstream),
            UpstreamQueueTarget(id="opus-b", client_provider=FakeUpstream),
            UpstreamQueueTarget(id="opus-c", client_provider=FakeUpstream),
        ],
        quota_manager=object(),
        usage_logs=object(),
        max_queue_size=7,
    )

    assert queue._dispatch_queue.maxsize == 21


def test_dispatch_queue_full_raises_queue_full():
    async def run_test():
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=FakeUpstream)],
            quota_manager=object(),
            usage_logs=object(),
            max_queue_size=10,
            dispatch_max_queue_size=1,
        )
        first = asyncio.create_task(
            queue.submit(
                request_id="queued",
                user_id=1,
                tier="normal",
                action="generate",
                logging_config=object(),
                estimated_cost=0,
                handler=lambda upstream: upstream.generate_image_payload_zip(PAYLOAD),
            )
        )
        await asyncio.sleep(0)
        try:
            try:
                await queue.submit(
                    request_id="overflow",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip(PAYLOAD),
                )
            except QueueFull:
                pass
            else:
                raise AssertionError("expected QueueFull")
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    asyncio.run(run_test())


def _queued_user_names(client: TestClient) -> list[str]:
    queue = client.get("/admin/api/queue", auth=("admin", "admin123")).json()
    return [item["user_name"] for item in queue["queued"]]


def _wait_until(predicate, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")
