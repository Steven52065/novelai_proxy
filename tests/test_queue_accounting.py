from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import LoggingConfig
from app.database import Database, utc_now_iso
from app.free_small_daily_limit import FreeSmallDailyLimitManager
from app.queue_manager import RoutingProxyQueue, UpstreamQueueTarget
from app.quota_manager import QuotaManager
from app.request_accounting import RequestAccounting
from app.usage_logs import UsageLogCreate, UsageLogRepository
from queue_manager_helpers import _wait_until_async


class ControlledSuccessUpstream:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def generate_image_payload_zip(self, payload):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return b"generated"


class ImmediateSuccessUpstream:
    def __init__(self):
        self.calls: list[dict] = []

    async def generate_image_payload_zip(self, payload):
        self.calls.append(payload)
        return b"generated"


def test_cancel_before_upstream_execution_releases_reserved_accounting(tmp_path: Path):
    async def run_test():
        context = _accounting_context(tmp_path)
        upstream = ControlledSuccessUpstream()
        queue = _queue(context, upstream)
        future = queue.enqueue(
            request_id="cancel-before-run",
            user_id=context["user_id"],
            tier="normal",
            action="generate",
            logging_config=LoggingConfig(),
            estimated_cost=3,
            handler=lambda upstream: upstream.generate_image_payload_zip({}),
            process_zip_response=False,
            accounting=_accounting(context, "cancel-before-run"),
        )
        future.cancel()

        queue.start()
        try:
            await _wait_until_async(lambda: context["quota"].get_snapshot(context["user_id"]).reserved == 0)
            quota = context["quota"].get_snapshot(context["user_id"])
            daily = context["daily"].get_snapshot(context["user_id"])
            log = context["usage_logs"].get_by_request_id("cancel-before-run")

            assert upstream.calls == 0
            assert quota.used == 0
            assert quota.reserved == 0
            assert daily.used == 0
            assert daily.reserved == 0
            assert log["status"] == "failed"
            assert log["error_code"] == "client_cancelled"
        finally:
            await queue.stop()
            context["db"].close()

    asyncio.run(run_test())


def test_cancel_during_upstream_interval_does_not_execute_or_charge(tmp_path: Path):
    async def run_test():
        context = _accounting_context(tmp_path)
        upstream = ImmediateSuccessUpstream()
        queue = _queue(context, upstream, upstream_interval_seconds=0.2)
        queue.start()
        try:
            first = queue.enqueue(
                request_id="warm-up",
                user_id=context["user_id"],
                tier="normal",
                action="generate",
                logging_config=LoggingConfig(),
                estimated_cost=0,
                handler=lambda upstream: upstream.generate_image_payload_zip({"request": "warm-up"}),
                process_zip_response=False,
                manage_quota=False,
            )
            assert await asyncio.wait_for(first, timeout=1) == b"generated"

            second = queue.enqueue(
                request_id="cancel-during-interval",
                user_id=context["user_id"],
                tier="normal",
                action="generate",
                logging_config=LoggingConfig(),
                estimated_cost=3,
                handler=lambda upstream: upstream.generate_image_payload_zip({"request": "cancel-during-interval"}),
                process_zip_response=False,
                accounting=_accounting(context, "cancel-during-interval"),
            )
            await _wait_until_async(
                lambda: queue._queues["opus-a"].running_item is not None
                and queue._queues["opus-a"].running_item.request_id == "cancel-during-interval"
            )

            second.cancel()
            await _wait_until_async(lambda: context["daily"].get_snapshot(context["user_id"]).reserved == 0)
            quota = context["quota"].get_snapshot(context["user_id"])
            daily = context["daily"].get_snapshot(context["user_id"])
            log = context["usage_logs"].get_by_request_id("cancel-during-interval")

            assert upstream.calls == [{"request": "warm-up"}]
            assert quota.used == 0
            assert quota.reserved == 0
            assert daily.used == 0
            assert daily.reserved == 0
            assert log["status"] == "failed"
            assert log["error_code"] == "client_cancelled"
        finally:
            await queue.stop()
            context["db"].close()

    asyncio.run(run_test())


def test_cancel_after_upstream_started_confirms_accounting_on_success(tmp_path: Path):
    async def run_test():
        context = _accounting_context(tmp_path)
        upstream = ControlledSuccessUpstream()
        queue = _queue(context, upstream)
        queue.start()
        try:
            future = queue.enqueue(
                request_id="cancel-after-start",
                user_id=context["user_id"],
                tier="normal",
                action="generate",
                logging_config=LoggingConfig(),
                estimated_cost=3,
                handler=lambda upstream: upstream.generate_image_payload_zip({}),
                process_zip_response=False,
                accounting=_accounting(context, "cancel-after-start"),
            )
            await _wait_until_async(upstream.started.is_set)

            future.cancel()
            await asyncio.sleep(0)

            quota_while_running = context["quota"].get_snapshot(context["user_id"])
            daily_while_running = context["daily"].get_snapshot(context["user_id"])
            assert quota_while_running.used == 0
            assert quota_while_running.reserved == 3
            assert daily_while_running.used == 0
            assert daily_while_running.reserved == 1

            upstream.release.set()
            await _wait_until_async(lambda: context["daily"].get_snapshot(context["user_id"]).used == 1)
            quota = context["quota"].get_snapshot(context["user_id"])
            daily = context["daily"].get_snapshot(context["user_id"])
            log = context["usage_logs"].get_by_request_id("cancel-after-start")

            assert quota.used == 3
            assert quota.reserved == 0
            assert daily.used == 1
            assert daily.reserved == 0
            assert log["status"] == "success"
        finally:
            upstream.release.set()
            await queue.stop()
            context["db"].close()

    asyncio.run(run_test())


def _accounting_context(tmp_path: Path) -> dict:
    db = Database(str(tmp_path / "queue-accounting.db"))
    db.init_schema()
    quota = QuotaManager(db)
    daily = FreeSmallDailyLimitManager(db)
    usage_logs = UsageLogRepository(db)
    user_id = _create_user(db)
    quota.create_or_update(user_id, 10)
    quota.reserve(user_id, 3)
    daily_reservation = daily.reserve(user_id, 1)
    usage_logs.insert_queued(
        UsageLogCreate(
            request_id="cancel-before-run",
            user_id=user_id,
            action="generate",
            estimated_anlas_cost=3,
        )
    )
    usage_logs.insert_queued(
        UsageLogCreate(
            request_id="cancel-after-start",
            user_id=user_id,
            action="generate",
            estimated_anlas_cost=3,
        )
    )
    usage_logs.insert_queued(
        UsageLogCreate(
            request_id="cancel-during-interval",
            user_id=user_id,
            action="generate",
            estimated_anlas_cost=3,
        )
    )
    return {
        "db": db,
        "quota": quota,
        "daily": daily,
        "daily_reservation": daily_reservation,
        "usage_logs": usage_logs,
        "user_id": user_id,
    }


def _accounting(context: dict, request_id: str) -> RequestAccounting:
    # 模拟 service 层在预检通过后创建的记账对象：额度与每日预约都已 reserved。
    return RequestAccounting(
        quota_manager=context["quota"],
        usage_logs=context["usage_logs"],
        request_id=request_id,
        user_id=context["user_id"],
        estimated_cost=3,
        free_small_daily_limit_manager=context["daily"],
        free_small_daily_reservation=context["daily_reservation"],
    )


def _create_user(db: Database) -> int:
    cursor = db.execute(
        """
        INSERT INTO users (
            api_key_hash, name, tier, is_active, free_small_only,
            free_small_daily_limit_enabled, free_small_daily_limit,
            allowed_endpoints, created_at
        )
        VALUES (?, 'queue-accounting-user', 'normal', 1, 0, 1, 1, 'generate-image', ?)
        """,
        ("queue-accounting-key", utc_now_iso()),
    )
    return int(cursor.lastrowid)


def _queue(context: dict, upstream, *, upstream_interval_seconds: float = 0) -> RoutingProxyQueue:
    return RoutingProxyQueue(
        targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream)],
        quota_manager=context["quota"],
        usage_logs=context["usage_logs"],
        max_queue_size=2,
        upstream_interval_min_seconds=upstream_interval_seconds,
        upstream_interval_max_seconds=upstream_interval_seconds,
        upstream_error_extra_delay_seconds=0,
    )
