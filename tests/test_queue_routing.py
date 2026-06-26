from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient
from novelai_python._exceptions import APIError
import pytest

from helpers import PAYLOAD, BlockingFakeUpstream, FakeUpstream, write_test_config_with_upstreams
from app.queue_manager import RoutingProxyQueue, UpstreamQueueTarget


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

def test_adaptive_weighted_random_lowers_failed_upstream_weight(monkeypatch):
    async def run_test():
        queue = RoutingProxyQueue(
            targets=[
                UpstreamQueueTarget(id="opus-a", client_provider=FakeUpstream),
                UpstreamQueueTarget(id="opus-b", client_provider=FakeUpstream),
            ],
            quota_manager=object(),
            usage_logs=object(),
            max_queue_size=10,
            routing_strategy="adaptive_weighted_random",
            adaptive_initial_score=0.8,
            adaptive_alpha=0.5,
            adaptive_min_weight=0.15,
        )
        monkeypatch.setattr("app.queue_manager.random.uniform", lambda _start, _end: 0.56)

        assert queue._candidate_upstreams(None, advance_round_robin=True)[0] == "opus-a"

        failed = asyncio.get_running_loop().create_future()
        failed.set_exception(RuntimeError("upstream failed"))
        queue._record_adaptive_result("opus-a", failed)

        assert queue._candidate_upstreams(None, advance_round_robin=True)[0] == "opus-b"

    asyncio.run(run_test())

def test_adaptive_weighted_random_keeps_minimum_weight_for_failed_upstream(monkeypatch):
    queue = RoutingProxyQueue(
        targets=[
            UpstreamQueueTarget(id="opus-a", client_provider=FakeUpstream),
            UpstreamQueueTarget(id="opus-b", client_provider=FakeUpstream),
        ],
        quota_manager=object(),
        usage_logs=object(),
        max_queue_size=10,
        routing_strategy="adaptive_weighted_random",
        adaptive_initial_score=0,
        adaptive_alpha=0.5,
        adaptive_min_weight=0.15,
    )
    queue._adaptive_scores["opus-b"].score = 1
    monkeypatch.setattr("app.queue_manager.random.uniform", lambda _start, _end: 0.1)

    assert queue._candidate_upstreams(None, advance_round_robin=True)[0] == "opus-a"

def test_select_client_does_not_use_adaptive_weighted_random_for_query_routes(monkeypatch):
    queue = RoutingProxyQueue(
        targets=[
            UpstreamQueueTarget(id="opus-a", client_provider=lambda: "client-a"),
            UpstreamQueueTarget(id="opus-b", client_provider=lambda: "client-b"),
        ],
        quota_manager=object(),
        usage_logs=object(),
        max_queue_size=10,
        routing_strategy="adaptive_weighted_random",
        adaptive_initial_score=0,
        adaptive_alpha=0.5,
        adaptive_min_weight=0.15,
    )
    queue._adaptive_scores["opus-b"].score = 1
    monkeypatch.setattr("app.queue_manager.random.uniform", lambda _start, _end: 0.2)

    assert queue.select_client() == "client-a"


def test_admin_upstream_probe_does_not_update_adaptive_weight_or_retry_429():
    class Always429Upstream(FakeUpstream):
        async def generate_image_payload_zip(self, payload):
            self.generate_started_at.append(time.monotonic())
            self.last_generate_payload = payload
            raise APIError(
                "Too many requests",
                request=payload,
                response={"message": "Too many requests"},
                code="429",
            )

    async def run_test():
        upstream_a = Always429Upstream()
        upstream_b = FakeUpstream()
        queue = RoutingProxyQueue(
            targets=[
                UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream_a),
                UpstreamQueueTarget(id="opus-b", client_provider=lambda: upstream_b),
            ],
            quota_manager=object(),
            usage_logs=object(),
            max_queue_size=2,
            routing_strategy="adaptive_weighted_random",
            adaptive_initial_score=0.8,
            adaptive_alpha=0.5,
            upstream_error_extra_delay_seconds=0,
        )
        queue.start()
        try:
            before = queue.get_weights()
            with pytest.raises(APIError):
                await queue.submit_upstream_probe(
                    upstream_id="opus-a",
                    request_id="admin-probe-429",
                    logging_config=object(),
                    handler=lambda upstream: upstream.generate_image_payload_zip(PAYLOAD),
                )
            after = queue.get_weights()
        finally:
            await queue.stop()

        assert len(upstream_a.generate_started_at) == 1
        assert len(upstream_b.generate_started_at) == 0
        assert after["upstreams"][0]["score"] == before["upstreams"][0]["score"]

    asyncio.run(run_test())
