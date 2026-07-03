from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi.testclient import TestClient

from helpers import PAYLOAD, FailingThenSuccessfulUpstream, FakeUpstream, write_test_config
from app.upstream_queue import ProxyQueue
from queue_manager_helpers import _NoopUsageLogs


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
        retry_gap = fake_upstream.generate_started_at[1] - fake_upstream.generate_started_at[0]
        # Keep enough tolerance for Windows scheduler jitter while still proving the 0.05s error delay was applied.
        assert retry_gap >= 0.055

def test_interval_delay_counts_idle_time():
    async def run_test():
        queue = ProxyQueue(
            upstream_id="opus-a",
            usage_logs=_NoopUsageLogs(),
            max_queue_size=2,
            upstream_interval_min_seconds=0.05,
            upstream_interval_max_seconds=0.05,
            upstream_error_extra_delay_seconds=0,
        )
        queue._last_upstream_completed_at = time.monotonic() - 0.2

        started_at = time.monotonic()
        await queue._wait_for_upstream_interval("after-idle")
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.03

    asyncio.run(run_test())

def test_error_extra_delay_counts_idle_time():
    async def run_test():
        queue = ProxyQueue(
            upstream_id="opus-a",
            usage_logs=_NoopUsageLogs(),
            max_queue_size=2,
            upstream_interval_min_seconds=0.05,
            upstream_interval_max_seconds=0.05,
            upstream_error_extra_delay_seconds=0.2,
        )
        queue._last_upstream_completed_at = time.monotonic() - 0.35
        queue._apply_error_extra_delay_next = True

        started_at = time.monotonic()
        await queue._wait_for_upstream_interval("after-idle-error")
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.03

    asyncio.run(run_test())
