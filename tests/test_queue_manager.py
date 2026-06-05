from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient
from novelai_python._exceptions import APIError

from helpers import (
    PAYLOAD,
    BlockingFakeUpstream,
    FailingThenSuccessfulUpstream,
    FakeQueueSnapshot,
    FakeUpstream,
    write_test_config,
    write_test_config_with_upstreams,
)
from app.queue_manager import (
    DispatchQueueItem,
    QueueClosed,
    QueueFull,
    Retry429Error,
    RoutingProxyQueue,
    UpstreamExecutionTimeout,
    UpstreamQueueTarget,
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

        assert first.status_code == 201
        assert second.status_code == 201
        assert len(fake_upstream.generate_started_at) == 3
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


def test_dispatcher_does_not_wait_for_upstream_completion():
    async def run_test():
        release_a = threading.Event()
        queue = RoutingProxyQueue(
            targets=[
                UpstreamQueueTarget(id="opus-a", client_provider=lambda: BlockingFakeUpstream(release_a)),
                UpstreamQueueTarget(id="opus-b", client_provider=FakeUpstream),
            ],
            quota_manager=object(),
            usage_logs=_NoopUsageLogs(),
            max_queue_size=2,
        )
        queue.start()
        try:
            first = asyncio.create_task(
                queue.submit(
                    request_id="blocked-a",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip(PAYLOAD),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: queue._queues["opus-a"]._running_item is not None)

            second = await asyncio.wait_for(
                queue.submit(
                    request_id="fast-b",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip(PAYLOAD),
                    process_zip_response=False,
                    manage_quota=False,
                ),
                timeout=1,
            )
            assert b"fake-image" in second
            assert queue._queues["opus-b"]._last_upstream_completed_at is not None

            release_a.set()
            assert b"fake-image" in await asyncio.wait_for(first, timeout=1)
        finally:
            release_a.set()
            await queue.stop()

    asyncio.run(run_test())


def test_upstream_execution_timeout_excludes_interval_and_frees_queue():
    async def run_test():
        upstream = TimeoutByLabelUpstream(timeout_label="slow")
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream)],
            quota_manager=object(),
            usage_logs=_NoopUsageLogs(),
            max_queue_size=2,
            upstream_interval_min_seconds=0.05,
            upstream_interval_max_seconds=0.05,
            upstream_error_extra_delay_seconds=0,
            upstream_execution_timeout_seconds=0.02,
        )
        queue.start()
        try:
            first = await queue.submit(
                request_id="fast-first",
                user_id=1,
                tier="normal",
                action="generate",
                logging_config=object(),
                estimated_cost=0,
                handler=lambda upstream: upstream.generate_image_payload_zip({"label": "fast"}),
                process_zip_response=False,
                manage_quota=False,
            )
            assert first == b"fast"

            slow = asyncio.create_task(
                queue.submit(
                    request_id="slow-timeout",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "slow"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            try:
                await asyncio.wait_for(slow, timeout=1)
            except UpstreamExecutionTimeout as exc:
                assert exc.timeout_seconds == 0.02
            else:
                raise AssertionError("expected upstream execution timeout")

            assert upstream.started_labels == ["fast", "slow"]
            assert upstream.started_at[1] - upstream.started_at[0] >= 0.045

            after = await asyncio.wait_for(
                queue.submit(
                    request_id="after-timeout",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "after"}),
                    process_zip_response=False,
                    manage_quota=False,
                ),
                timeout=1,
            )
            assert after == b"after"
            assert upstream.started_labels == ["fast", "slow", "after"]
        finally:
            await queue.stop()

    asyncio.run(run_test())


def test_normal_429_retry_returns_to_queue_tail():
    async def run_test():
        release_first = threading.Event()
        upstream = One429ThenSuccessfulUpstream(release_first, fail_label="normal-first")
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream)],
            quota_manager=object(),
            usage_logs=_NoopUsageLogs(),
            max_queue_size=2,
            upstream_interval_min_seconds=0,
            upstream_interval_max_seconds=0,
            upstream_error_extra_delay_seconds=0,
        )
        queue.start()
        try:
            first = asyncio.create_task(
                queue.submit(
                    request_id="normal-first",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "normal-first"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: upstream.started_labels == ["normal-first"])

            second = asyncio.create_task(
                queue.submit(
                    request_id="normal-second",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "normal-second"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: queue._queues["opus-a"].qsize() == 1)

            release_first.set()

            assert b"normal-first" in await asyncio.wait_for(first, timeout=2)
            assert b"normal-second" in await asyncio.wait_for(second, timeout=2)
            assert upstream.started_labels == ["normal-first", "normal-second", "normal-first"]
        finally:
            release_first.set()
            await queue.stop()

    asyncio.run(run_test())


def test_vip_429_retry_goes_to_upstream_queue_front():
    async def run_test():
        release_first = threading.Event()
        upstream = One429ThenSuccessfulUpstream(release_first, fail_label="vip-first")
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream)],
            quota_manager=object(),
            usage_logs=_NoopUsageLogs(),
            max_queue_size=2,
            upstream_interval_min_seconds=0,
            upstream_interval_max_seconds=0,
            upstream_error_extra_delay_seconds=0,
        )
        queue.start()
        try:
            first = asyncio.create_task(
                queue.submit(
                    request_id="vip-first",
                    user_id=1,
                    tier="vip",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "vip-first"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: upstream.started_labels == ["vip-first"])

            second = asyncio.create_task(
                queue.submit(
                    request_id="normal-second",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "normal-second"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: queue._queues["opus-a"].qsize() == 1)

            release_first.set()

            assert b"vip-first" in await asyncio.wait_for(first, timeout=2)
            assert b"normal-second" in await asyncio.wait_for(second, timeout=2)
            assert upstream.started_labels == ["vip-first", "vip-first", "normal-second"]
        finally:
            release_first.set()
            await queue.stop()

    asyncio.run(run_test())


def test_vip_request_goes_to_upstream_queue_front_even_when_full():
    async def run_test():
        release_first = threading.Event()
        upstream = FirstRequestBlockingLabelUpstream(release_first)
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream)],
            quota_manager=object(),
            usage_logs=_NoopUsageLogs(),
            max_queue_size=1,
            upstream_interval_min_seconds=0,
            upstream_interval_max_seconds=0,
            upstream_error_extra_delay_seconds=0,
        )
        queue.start()
        try:
            first = asyncio.create_task(
                queue.submit(
                    request_id="normal-first",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "normal-first"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: upstream.started_labels == ["normal-first"])

            second = asyncio.create_task(
                queue.submit(
                    request_id="normal-second",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "normal-second"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: queue._queues["opus-a"].qsize() == 1)

            vip = asyncio.create_task(
                queue.submit(
                    request_id="vip-third",
                    user_id=1,
                    tier="vip",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "vip-third"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: queue._queues["opus-a"].qsize() == 2)

            release_first.set()

            assert b"normal-first" in await asyncio.wait_for(first, timeout=2)
            assert b"vip-third" in await asyncio.wait_for(vip, timeout=2)
            assert b"normal-second" in await asyncio.wait_for(second, timeout=2)
            assert upstream.started_labels == ["normal-first", "vip-third", "normal-second"]
        finally:
            release_first.set()
            await queue.stop()

    asyncio.run(run_test())


def test_stop_drains_accepted_requests_and_rejects_new_submissions():
    async def run_test():
        release_first = threading.Event()
        upstream = FirstRequestBlockingLabelUpstream(release_first)
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream)],
            quota_manager=object(),
            usage_logs=_NoopUsageLogs(),
            max_queue_size=2,
            upstream_interval_min_seconds=0,
            upstream_interval_max_seconds=0,
            upstream_error_extra_delay_seconds=0,
        )
        queue.start()
        stop_task = None
        try:
            first = asyncio.create_task(
                queue.submit(
                    request_id="shutdown-first",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "shutdown-first"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: upstream.started_labels == ["shutdown-first"])

            second = asyncio.create_task(
                queue.submit(
                    request_id="shutdown-second",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "shutdown-second"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: queue._queues["opus-a"].qsize() == 1)

            stop_task = asyncio.create_task(queue.stop())
            await _wait_until_async(lambda: not queue._accepting)
            assert not stop_task.done()

            try:
                await queue.submit(
                    request_id="shutdown-rejected",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "shutdown-rejected"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            except QueueClosed:
                pass
            else:
                raise AssertionError("expected QueueClosed")

            release_first.set()
            assert b"shutdown-first" in await asyncio.wait_for(first, timeout=2)
            assert b"shutdown-second" in await asyncio.wait_for(second, timeout=2)
            await asyncio.wait_for(stop_task, timeout=2)
            assert upstream.started_labels == ["shutdown-first", "shutdown-second"]
        finally:
            release_first.set()
            if stop_task is None:
                await queue.stop()
            elif stop_task.done():
                await asyncio.gather(stop_task, return_exceptions=True)
            elif not stop_task.done():
                await asyncio.wait_for(stop_task, timeout=2)

    asyncio.run(run_test())


def test_cancelled_429_retry_releases_reserved_quota():
    async def run_test():
        quota = _RecordingQuota()
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=FakeUpstream)],
            quota_manager=quota,
            usage_logs=_NoopUsageLogs(),
            max_queue_size=2,
        )
        loop = asyncio.get_running_loop()
        client_future = loop.create_future()
        client_future.cancel()
        completed = loop.create_future()
        completed.set_exception(Retry429Error(_api_429()))
        item = DispatchQueueItem(
            priority=RoutingProxyQueue.NORMAL_PRIORITY,
            sequence=0,
            enqueued_at=time.monotonic(),
            request_id="cancelled-retry",
            user_id=42,
            action="generate",
            tier="normal",
            estimated_cost=7,
            manage_quota=True,
            logging_config=object(),
            process_zip_response=False,
            allowed_upstreams=None,
            handler=lambda upstream: upstream.generate_image_payload_zip(PAYLOAD),
            future=client_future,
        )

        queue._handle_upstream_completion(
            completed,
            item=item,
            upstream_id="opus-a",
            last_429_error=None,
        )

        assert quota.released == [(42, 7)]

    asyncio.run(run_test())


def test_cancelled_before_dispatch_marks_failed_log_and_releases_quota():
    async def run_test():
        quota = _RecordingQuota()
        usage_logs = _RecordingUsageLogs()
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=FakeUpstream)],
            quota_manager=quota,
            usage_logs=usage_logs,
            max_queue_size=2,
        )
        task = asyncio.create_task(
            queue.submit(
                request_id="cancel-before-dispatch",
                user_id=42,
                tier="normal",
                action="generate",
                logging_config=object(),
                estimated_cost=7,
                handler=lambda upstream: upstream.generate_image_payload_zip(PAYLOAD),
                process_zip_response=False,
            )
        )
        await _wait_until_async(lambda: queue._dispatch_queue.qsize() == 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        queue.start()
        try:
            await _wait_until_async(lambda: quota.released == [(42, 7)] and len(usage_logs.failed) == 1)
        finally:
            await queue.stop()

        assert usage_logs.failed == [
            {
                "request_id": "cancel-before-dispatch",
                "error_code": "client_cancelled",
                "error_message": "Client cancelled before dispatch",
                "attempt_number": 0,
            }
        ]

    asyncio.run(run_test())


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


async def _wait_until_async(predicate, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


class _NoopUsageLogs:
    def mark_running(self, *args, **kwargs):
        pass

    def mark_success(self, *args, **kwargs):
        pass

    def mark_failed(self, *args, **kwargs):
        pass

    def insert_retry_attempt(self, *args, **kwargs):
        pass

    def update_image_urls(self, *args, **kwargs):
        pass


class One429ThenSuccessfulUpstream(FakeUpstream):
    def __init__(self, first_release_event: threading.Event, *, fail_label: str):
        super().__init__()
        self.first_release_event = first_release_event
        self.fail_label = fail_label
        self.failed_once = False
        self.started_labels = []

    async def generate_image_payload_zip(self, payload):
        label = payload["label"]
        self.started_labels.append(label)
        if label == self.fail_label and not self.failed_once:
            self.failed_once = True
            await asyncio.to_thread(self.first_release_event.wait)
            raise APIError(
                "Too many requests",
                request=payload,
                response={"message": "Too many requests"},
                code="429",
            )
        return label.encode("utf-8")


class FirstRequestBlockingLabelUpstream(FakeUpstream):
    def __init__(self, first_release_event: threading.Event):
        super().__init__()
        self.first_release_event = first_release_event
        self.started_labels = []

    async def generate_image_payload_zip(self, payload):
        label = payload["label"]
        self.started_labels.append(label)
        if len(self.started_labels) == 1:
            await asyncio.to_thread(self.first_release_event.wait)
        return label.encode("utf-8")


class TimeoutByLabelUpstream(FakeUpstream):
    def __init__(self, *, timeout_label: str):
        super().__init__()
        self.timeout_label = timeout_label
        self.started_labels = []
        self.started_at = []

    async def generate_image_payload_zip(self, payload):
        label = payload["label"]
        self.started_labels.append(label)
        self.started_at.append(time.monotonic())
        if label == self.timeout_label:
            await asyncio.Event().wait()
        return label.encode("utf-8")


class _RecordingQuota:
    def __init__(self):
        self.released = []

    def release(self, user_id: int, cost: int):
        self.released.append((user_id, cost))


class _RecordingUsageLogs(_NoopUsageLogs):
    def __init__(self):
        self.failed = []

    def mark_failed(self, request_id, *, queued_ms, error_code, error_message, upstream_ms=None, attempt_number=0):
        self.failed.append(
            {
                "request_id": request_id,
                "error_code": error_code,
                "error_message": error_message,
                "attempt_number": attempt_number,
            }
        )


def _api_429() -> APIError:
    return APIError(
        "Too many requests",
        request={},
        response={"message": "Too many requests"},
        code="429",
    )
