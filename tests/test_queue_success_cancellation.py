from __future__ import annotations

import asyncio
from pathlib import Path
from sqlite3 import OperationalError

import pytest

from app.api_errors import APIError
from app.config import LoggingConfig
from app.database import Database, utc_now_iso
from app.free_small_daily_limit import FreeSmallDailyLimitManager
from app.idle_free_small import IdleFreeSmallContext, IdleFreeSmallTracker
from app.idle_free_small_daily_limit import IdleFreeSmallDailyLimitManager
from app.queue_models import UpstreamQueueTarget
from app.quota_manager import QuotaManager
from app.request_accounting import RequestAccounting
from app.routing_queue import RoutingProxyQueue
from app.usage_logs import UsageLogCreate, UsageLogRepository


@pytest.fixture
def cancellation_request(tmp_path: Path, pool):
    db = Database(str(tmp_path / "success-cancellation.db"))
    db.init_schema()
    try:
        user_id = int(db.execute(
            "INSERT INTO users (api_key_hash, name, is_active, free_small_daily_limit_enabled, "
            "free_small_daily_limit, idle_free_small_multiplier, created_at) VALUES (?, ?, 1, 1, 1, 1, ?)",
            ("success-cancellation", "success-cancellation", utc_now_iso()),
        ).lastrowid)
        quota = QuotaManager(db)
        quota.create_or_update(user_id, total=100)
        normal = FreeSmallDailyLimitManager(db)
        idle = IdleFreeSmallDailyLimitManager(db)
        logs = UsageLogRepository(db)
        cost = 7 if pool == "anlas" else 0
        quota.reserve(user_id, cost)
        accounting = RequestAccounting(
            quota_manager=quota, usage_logs=logs, request_id="cancelled", user_id=user_id, estimated_cost=cost,
            free_small_daily_limit_manager=normal,
            free_small_daily_reservation=normal.reserve(user_id, 1) if pool == "normal" else None,
            idle_free_small_daily_limit_manager=idle,
            idle_free_small_reservation=idle.try_reserve(user_id) if pool == "idle" else None,
        )
        logs.insert_queued(UsageLogCreate(
            request_id="cancelled", user_id=user_id, action="generate", estimated_anlas_cost=cost,
        ))
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: None)],
            quota_manager=quota,
            usage_logs=logs,
            max_queue_size=2,
            upstream_interval_min_seconds=0,
            upstream_interval_max_seconds=0,
            upstream_error_extra_delay_seconds=0,
            tracker=IdleFreeSmallTracker(min_idle_seconds=0),
        )

        def enqueue(handler):
            return queue.enqueue(
                request_id="cancelled", user_id=user_id, tier="normal", action="generate",
                logging_config=LoggingConfig(), estimated_cost=cost, handler=handler, process_zip_response=True,
                accounting=accounting,
                idle_free_small=IdleFreeSmallContext(normal.get_snapshot(user_id), 1) if pool == "idle" else None,
            )

        def snapshot_totals():
            return [(snapshot.used, snapshot.reserved) for snapshot in (
                normal.get_snapshot(user_id), idle.get_snapshot(user_id), quota.get_snapshot(user_id),
            )]

        yield queue, accounting, enqueue, snapshot_totals
    finally:
        db.close()


@pytest.mark.parametrize("pool", ["normal", "idle", "anlas"])
@pytest.mark.parametrize("cancel_stage", ["archive", "handler_completed"])
@pytest.mark.parametrize("success_log_fails", [False, True])
@pytest.mark.parametrize("retry_first", [False, True])
def test_cancellation_after_upstream_success_confirms_accounting(
    cancellation_request, monkeypatch, pool, cancel_stage, success_log_fails, retry_first,
):
    async def run_test():
        queue, accounting, enqueue, snapshot_totals = cancellation_request
        upstream_queue = queue._queues["opus-a"]
        reached_cancel_stage = asyncio.Event()
        handler_calls = 0
        success_log_calls = []
        payload = b"generated-image"

        async def handler(_upstream):
            nonlocal handler_calls
            handler_calls += 1
            if retry_first and handler_calls == 1:
                raise APIError("Too many requests", request={}, response={}, code="429")
            if cancel_stage == "handler_completed":
                # 在 handler 返回的同一轮事件循环取消 worker，覆盖 wait 尚未返回的竞态。
                upstream_queue._worker.cancel()
                reached_cancel_stage.set()
            return payload

        async def blocked_archive(_item, _payload):
            reached_cancel_stage.set()
            await asyncio.Event().wait()

        real_mark_success = accounting.usage_logs.mark_success

        def mark_success(*args, **kwargs):
            success_log_calls.append(kwargs)
            if success_log_fails:
                raise OperationalError("injected success log failure")
            return real_mark_success(*args, **kwargs)

        monkeypatch.setattr(upstream_queue, "_archive_successful_zip_images", blocked_archive)
        monkeypatch.setattr(accounting.usage_logs, "mark_success", mark_success)
        queue.start()
        try:
            future = enqueue(handler)
            await asyncio.wait_for(reached_cancel_stage.wait(), timeout=1)
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)

            assert snapshot_totals() == [
                (1 if pool == "normal" else 0, 0),
                (1 if pool == "idle" else 0, 0),
                (7 if pool == "anlas" else 0, 0),
            ]
            assert accounting.settled
            assert await asyncio.wait_for(future, timeout=1) == payload
            assert handler_calls == (2 if retry_first else 1)
            assert len(success_log_calls) == 1
            success_log = success_log_calls[0]
            assert success_log["attempt_number"] == int(retry_first)
            assert success_log["is_retry_success"] == retry_first
            assert success_log["queued_ms"] >= 0
            assert success_log["upstream_ms"] >= 0
            assert success_log["output_files"] == []

            rows = accounting.usage_logs.db.query_all(
                "SELECT status, error_code, final_anlas_cost, is_retry_success "
                "FROM usage_logs WHERE request_id = ? ORDER BY attempt_number", ("cancelled",),
            )
            assert len(rows) == handler_calls
            if retry_first:
                assert (rows[0]["status"], rows[0]["error_code"]) == ("failed", "429")
            assert rows[-1]["status"] == ("running" if success_log_fails else "success")
            assert rows[-1]["error_code"] is None
            if not success_log_fails:
                assert rows[-1]["final_anlas_cost"] == accounting.estimated_cost
                assert rows[-1]["is_retry_success"] == int(retry_first)
            assert upstream_queue._worker.cancelled()
            assert upstream_queue.running_item is None
            await asyncio.wait_for(upstream_queue.queue.join(), timeout=1)
            assert not queue._tracker.running_sources
            assert not queue._active_futures
        finally:
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)

    asyncio.run(run_test())


@pytest.mark.parametrize("pool", ["normal", "idle", "anlas"])
@pytest.mark.parametrize("upstream_outcome", ["pending", "error", "cancelled"])
def test_worker_cancellation_without_success_releases_accounting(cancellation_request, upstream_outcome):
    async def run_test():
        queue, accounting, enqueue, snapshot_totals = cancellation_request
        upstream_queue = queue._queues["opus-a"]
        handler_started = asyncio.Event()

        async def handler(_upstream):
            handler_started.set()
            if upstream_outcome == "pending":
                await asyncio.Event().wait()
            upstream_queue._worker.cancel()
            if upstream_outcome == "error":
                raise RuntimeError("upstream failed before worker cancellation")
            raise asyncio.CancelledError

        queue.start()
        try:
            future = enqueue(handler)
            await asyncio.wait_for(handler_started.wait(), timeout=1)
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)
            assert snapshot_totals() == [(0, 0), (0, 0), (0, 0)]
            assert accounting.settled
            assert future.cancelled()
            assert accounting.usage_logs.get_by_request_id("cancelled")["status"] == "failed"
            assert upstream_queue._worker.cancelled()
            assert not queue._active_futures
        finally:
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)

    asyncio.run(run_test())
