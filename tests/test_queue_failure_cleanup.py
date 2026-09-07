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


@pytest.mark.parametrize("pool", ["normal", "idle", "anlas"])
@pytest.mark.parametrize(
    "failure_stage,persistent_log_failure",
    [
        ("dispatch", False),
        ("dispatch", True),
        ("retry_log", False),
        ("retry_log", True),
        ("retry_decision", False),
        ("retry_enqueue", False),
    ],
)
def test_terminal_queue_errors_release_reservations_and_allow_next_request(
    tmp_path: Path, monkeypatch, pool, failure_stage, persistent_log_failure
):
    async def run_test():
        db = Database(str(tmp_path / "failure-cleanup.db"))
        db.init_schema()
        user_id = int(db.execute(
            "INSERT INTO users (api_key_hash, name, is_active, free_small_daily_limit_enabled, "
            "free_small_daily_limit, idle_free_small_multiplier, created_at) VALUES (?, ?, 1, 1, 1, 1, ?)",
            ("failure-cleanup", "failure-cleanup", utc_now_iso()),
        ).lastrowid)
        quota = QuotaManager(db)
        quota.create_or_update(user_id, total=100)
        normal = FreeSmallDailyLimitManager(db)
        idle = IdleFreeSmallDailyLimitManager(db)
        logs = UsageLogRepository(db)
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
        queue.start()
        upstream_calls = []

        def enqueue(request_id):
            cost = 7 if pool == "anlas" else 0
            normal_reservation = normal.reserve(user_id, 1) if pool == "normal" else None
            idle_reservation = idle.try_reserve(user_id) if pool == "idle" else None
            quota.reserve(user_id, cost)
            logs.insert_queued(UsageLogCreate(
                request_id=request_id, user_id=user_id, action="generate", estimated_anlas_cost=cost,
            ))
            accounting = RequestAccounting(
                quota_manager=quota, usage_logs=logs, request_id=request_id, user_id=user_id, estimated_cost=cost,
                free_small_daily_limit_manager=normal, free_small_daily_reservation=normal_reservation,
                idle_free_small_daily_limit_manager=idle, idle_free_small_reservation=idle_reservation,
            )

            async def handler(_upstream):
                upstream_calls.append(request_id)
                if request_id == "failed" and failure_stage != "dispatch":
                    raise APIError("Too many requests", request={}, response={}, code="429")
                return b"generated-image"

            return queue.enqueue(
                request_id=request_id, user_id=user_id, tier="normal", action="generate",
                logging_config=LoggingConfig(), estimated_cost=cost, handler=handler, process_zip_response=False,
                accounting=accounting,
                idle_free_small=IdleFreeSmallContext(normal.get_snapshot(user_id), 1) if pool == "idle" else None,
            )

        def fail(*_args, **_kwargs):
            raise OperationalError("injected queue failure")

        real_mark_failed = logs.mark_failed
        failed_log_calls = 0

        def fail_log(*args, **kwargs):
            nonlocal failed_log_calls
            failed_log_calls += 1
            if persistent_log_failure or failed_log_calls == 1:
                raise OperationalError("injected log failure")
            return real_mark_failed(*args, **kwargs)

        try:
            with monkeypatch.context() as patch:
                if failure_stage == "dispatch":
                    patch.setattr(queue, "_is_user_available", fail)
                elif failure_stage == "retry_decision":
                    patch.setattr(queue._retry_policy, "decide_retry", fail)
                elif failure_stage == "retry_enqueue":
                    patch.setattr(queue, "_requeue_429_retry", fail)
                if failure_stage == "retry_log" or persistent_log_failure:
                    patch.setattr(logs, "mark_failed", fail_log)

                future = enqueue("failed")
                error_type = APIError if failure_stage == "retry_log" else OperationalError
                with pytest.raises(error_type):
                    await asyncio.wait_for(future, timeout=1)
                await asyncio.wait_for(queue._dispatch_queue.join(), timeout=1)
                await asyncio.wait_for(queue._queues["opus-a"].queue.join(), timeout=1)

            assert future.done()
            assert upstream_calls == ([] if failure_stage == "dispatch" else ["failed"])
            for snapshot in (normal.get_snapshot(user_id), idle.get_snapshot(user_id), quota.get_snapshot(user_id)):
                assert (snapshot.used, snapshot.reserved) == (0, 0)
            rows = db.query_all("SELECT attempt_number, status, error_code FROM usage_logs WHERE request_id = ?", ("failed",))
            assert len(rows) == 1
            assert rows[0]["attempt_number"] == 0
            if not persistent_log_failure:
                assert rows[0]["status"] == "failed"
                assert rows[0]["error_code"] == ("OperationalError" if failure_stage == "dispatch" else "429")

            assert await asyncio.wait_for(enqueue("recovered"), timeout=1) == b"generated-image"
            assert normal.get_snapshot(user_id).used == (1 if pool == "normal" else 0)
            assert idle.get_snapshot(user_id).used == (1 if pool == "idle" else 0)
            assert quota.get_snapshot(user_id).used == (7 if pool == "anlas" else 0)
            assert logs.get_by_request_id("recovered")["status"] == "success"
            assert not queue._active_futures
        finally:
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)
            db.close()

    asyncio.run(run_test())
