
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from sqlite3 import OperationalError

import pytest

from app.api_errors import APIError
from app.config import LoggingConfig
from app.database import Database, utc_now_iso
from app.free_small_daily_limit import FreeSmallDailyLimitManager
from app.idle_free_small import IdleFreeSmallContext, IdleFreeSmallTracker
from app.idle_free_small_daily_limit import IdleFreeSmallDailyLimitManager
from app.queue_errors import IdleFreeSmallRejected
from app.quota_manager import QuotaManager
from app.request_accounting import RequestAccounting
from app.routing_queue import RoutingProxyQueue
from app.queue_models import UpstreamQueueTarget
from app.usage_logs import UsageLogCreate, UsageLogRepository


class ImmediateUpstream:
    def __init__(self):
        self.calls = 0

    async def generate_image_payload_zip(self, payload):
        self.calls += 1
        return b"idle-image"


class Retry429OnceUpstream:
    def __init__(self):
        self.calls = 0

    async def generate_image_payload_zip(self, payload):
        self.calls += 1
        raise APIError(
            "Too many requests",
            request=payload,
            response={"message": "Too many requests"},
            code="429",
        )


class BlockingUpstream:
    def __init__(self):
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate_image_payload_zip(self, payload):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return b"blocked-image"


def test_idle_free_small_request_succeeds_when_tracker_is_idle(tmp_path: Path):
    async def run_test():
        context = _idle_context(tmp_path, min_idle_seconds=0)
        upstream = ImmediateUpstream()
        queue = _proxy_queue(context, upstream, tracker=context["tracker"])
        queue.start()
        try:
            payload = await asyncio.wait_for(
                queue.enqueue(
                    request_id="idle-success",
                    user_id=context["user_id"],
                    tier="normal",
                    action="generate",
                    logging_config=LoggingConfig(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({}),
                    process_zip_response=False,
                    accounting=_accounting(context, "idle-success"),
                    idle_free_small=context["context"],
                ),
                timeout=1,
            )
            assert payload == b"idle-image"
            assert upstream.calls == 1
            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert snapshot.used == 1
            assert snapshot.reserved == 0
            log = context["usage_logs"].get_by_request_id("idle-success")
            assert log["status"] == "success"
        finally:
            await queue.stop()
            context["db"].close()

    asyncio.run(run_test())


def test_idle_free_small_request_rejected_when_worker_was_recently_busy(tmp_path: Path):
    async def run_test():
        context = _idle_context(tmp_path, min_idle_seconds=30)
        upstream = BlockingUpstream()
        queue = _proxy_queue(context, upstream, tracker=context["tracker"])
        queue.start()
        try:
            normal_future = queue.enqueue(
                request_id="normal-block",
                user_id=context["user_id"],
                tier="normal",
                action="generate",
                logging_config=LoggingConfig(),
                estimated_cost=0,
                handler=lambda upstream: upstream.generate_image_payload_zip({}),
                process_zip_response=False,
                manage_quota=False,
                accounting=RequestAccounting(
                    quota_manager=context["quota"],
                    usage_logs=context["usage_logs"],
                    request_id="normal-block",
                    user_id=context["user_id"],
                    estimated_cost=0,
                    manage_quota=False,
                ),
            )
            await asyncio.wait_for(upstream.started.wait(), timeout=1)

            idle_future = queue.enqueue(
                request_id="idle-rejected",
                user_id=context["user_id"],
                tier="normal",
                action="generate",
                logging_config=LoggingConfig(),
                estimated_cost=0,
                handler=lambda upstream: upstream.generate_image_payload_zip({}),
                process_zip_response=False,
                accounting=_accounting(context, "idle-rejected"),
                idle_free_small=context["context"],
            )
            upstream.release.set()
            assert await asyncio.wait_for(normal_future, timeout=1) == b"blocked-image"
            with pytest.raises(IdleFreeSmallRejected):
                await asyncio.wait_for(idle_future, timeout=1)

            assert upstream.calls == 1
            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert snapshot.used == 0
            assert snapshot.reserved == 0
            log = context["usage_logs"].get_by_request_id("idle-rejected")
            assert log["status"] == "rejected"
            assert log["error_code"] == "free_small_daily_limit_exceeded"
        finally:
            upstream.release.set()
            await queue.stop()
            context["db"].close()

    asyncio.run(run_test())


def test_idle_free_small_cancel_before_http_releases_reservation(tmp_path: Path):
    async def run_test():
        context = _idle_context(tmp_path, min_idle_seconds=0)
        upstream = ImmediateUpstream()
        queue = _proxy_queue(context, upstream, tracker=context["tracker"])
        queue.start()
        try:
            future = queue.enqueue(
                request_id="idle-cancel",
                user_id=context["user_id"],
                tier="normal",
                action="generate",
                logging_config=LoggingConfig(),
                estimated_cost=0,
                handler=lambda upstream: upstream.generate_image_payload_zip({}),
                process_zip_response=False,
                accounting=_accounting(context, "idle-cancel"),
                idle_free_small=context["context"],
            )
            future.cancel()
            deadline = 0
            while deadline < 100 and context["idle"].get_snapshot(context["user_id"]).reserved != 0:
                await asyncio.sleep(0.01)
                deadline += 1
            assert upstream.calls == 0
            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert snapshot.reserved == 0
            assert snapshot.used == 0
        finally:
            await queue.stop()
            context["db"].close()

    asyncio.run(run_test())


def test_idle_free_small_retry_rejected_when_tracker_is_not_idle(tmp_path: Path):
    async def run_test():
        context = _idle_context(tmp_path, min_idle_seconds=30)
        upstream = Retry429OnceUpstream()
        queue = _proxy_queue(context, upstream, tracker=context["tracker"])
        queue.start()
        context["clock"][0] = 30.0
        try:
            with pytest.raises(IdleFreeSmallRejected):
                await asyncio.wait_for(
                    queue.enqueue(
                        request_id="idle-retry",
                        user_id=context["user_id"],
                        tier="normal",
                        action="generate",
                        logging_config=LoggingConfig(),
                        estimated_cost=0,
                        handler=lambda upstream: upstream.generate_image_payload_zip({}),
                        process_zip_response=False,
                        accounting=_accounting(context, "idle-retry"),
                        idle_free_small=context["context"],
                    ),
                    timeout=1,
                )
            assert upstream.calls == 1
            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert snapshot.used == 0
            assert snapshot.reserved == 0
            log = context["usage_logs"].get_by_request_id("idle-retry")
            assert log["status"] == "failed"
            assert log["error_code"] == "429"
            assert log["attempt_number"] == 0
        finally:
            await queue.stop()
            context["db"].close()

    asyncio.run(run_test())


@pytest.mark.parametrize("log_write_fails", [False, True])
def test_idle_worker_precheck_error_releases_reservation_and_keeps_worker_alive(tmp_path: Path, monkeypatch, log_write_fails):
    async def run_test():
        context = _idle_context(tmp_path, min_idle_seconds=0)
        upstream = ImmediateUpstream()
        queue = _proxy_queue(context, upstream, tracker=context["tracker"])
        worker = queue._queues["opus-a"]
        queue.start()

        def fail_user_read(_user_id):
            raise OperationalError("user read failed")

        def fail_log_write(*_args, **_kwargs):
            raise OperationalError("log write failed")

        def enqueue(request_id):
            return queue.enqueue(
                request_id=request_id,
                user_id=context["user_id"],
                tier="normal",
                action="generate",
                logging_config=LoggingConfig(),
                estimated_cost=0,
                handler=lambda upstream: upstream.generate_image_payload_zip({}),
                process_zip_response=False,
                accounting=_accounting(context, request_id),
                idle_free_small=context["context"],
            )

        try:
            with monkeypatch.context() as patch:
                patch.setattr(worker, "_is_user_available", fail_user_read)
                if log_write_fails:
                    patch.setattr(context["usage_logs"], "mark_failed", fail_log_write)
                future = enqueue("idle-success")
                with pytest.raises(OperationalError, match="user read failed"):
                    await asyncio.wait_for(future, timeout=1)
                await asyncio.wait_for(worker.queue.join(), timeout=1)

            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert (snapshot.used, snapshot.reserved) == (0, 0)
            assert upstream.calls == 0
            assert future.done()
            assert not context["tracker"].running_sources
            if not log_write_fails:
                log = context["usage_logs"].get_by_request_id("idle-success")
                assert log["status"] == "failed"
                assert log["error_code"] == "OperationalError"

            context["reservation"] = context["idle"].try_reserve(context["user_id"])
            assert context["reservation"] is not None
            assert await asyncio.wait_for(enqueue("idle-rejected"), timeout=1) == b"idle-image"
            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert (snapshot.used, snapshot.reserved) == (1, 0)
            assert context["usage_logs"].get_by_request_id("idle-rejected")["status"] == "success"
        finally:
            await asyncio.wait_for(queue.stop(), timeout=1)
            context["db"].close()

    asyncio.run(run_test())


def test_reenabled_upstream_rejects_idle_work_until_old_worker_finishes(tmp_path: Path):
    async def run_test():
        context = _idle_context(tmp_path, min_idle_seconds=30)
        upstream = BlockingUpstream()
        queue = _proxy_queue(context, upstream, tracker=context["tracker"])
        context["usage_logs"].insert_queued(
            UsageLogCreate(request_id="old-worker", user_id=context["user_id"], action="generate", estimated_anlas_cost=0)
        )
        queue.start()

        def enqueue_idle(request_id):
            return queue.enqueue(
                request_id=request_id,
                user_id=context["user_id"],
                tier="normal",
                action="generate",
                logging_config=LoggingConfig(),
                estimated_cost=0,
                handler=lambda upstream: upstream.generate_image_payload_zip({}),
                process_zip_response=False,
                accounting=_accounting(context, request_id),
                idle_free_small=context["context"],
            )

        try:
            normal_future = queue.enqueue(
                request_id="old-worker",
                user_id=context["user_id"],
                tier="normal",
                action="generate",
                logging_config=LoggingConfig(),
                estimated_cost=0,
                handler=lambda upstream: upstream.generate_image_payload_zip({}),
                process_zip_response=False,
                manage_quota=False,
            )
            await asyncio.wait_for(upstream.started.wait(), timeout=1)
            old_worker = queue._queues["opus-a"]
            context["clock"][0] = 1
            queue.sync_targets([])
            queue.sync_targets([UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream)])
            context["clock"][0] = 31

            assert not queue.is_idle_free_available()
            rejected = enqueue_idle("idle-success")
            with pytest.raises(IdleFreeSmallRejected):
                await asyncio.wait_for(rejected, timeout=1)
            assert old_worker.running_item is not None
            assert upstream.calls == 1
            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert (snapshot.used, snapshot.reserved) == (0, 0)
            assert context["usage_logs"].get_by_request_id("idle-success")["status"] == "rejected"

            upstream.release.set()
            assert await asyncio.wait_for(normal_future, timeout=1) == b"blocked-image"
            assert old_worker._tracker_token not in context["tracker"].running_sources
            assert not queue.is_idle_free_available()
            context["clock"][0] = 61
            assert queue.is_idle_free_available()
            context["reservation"] = context["idle"].try_reserve(context["user_id"])
            assert context["reservation"] is not None
            assert await asyncio.wait_for(enqueue_idle("idle-rejected"), timeout=1) == b"blocked-image"
            assert upstream.calls == 2
            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert (snapshot.used, snapshot.reserved) == (1, 0)
        finally:
            upstream.release.set()
            await asyncio.wait_for(queue.stop(), timeout=1)
            context["db"].close()

    asyncio.run(run_test())


def _proxy_queue(context: dict, upstream, *, tracker: IdleFreeSmallTracker) -> RoutingProxyQueue:
    return RoutingProxyQueue(
        targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream)],
        quota_manager=context["quota"],
        usage_logs=context["usage_logs"],
        max_queue_size=2,
        upstream_interval_min_seconds=0,
        upstream_interval_max_seconds=0,
        upstream_error_extra_delay_seconds=0,
        tracker=tracker,
    )


def _idle_context(tmp_path: Path, *, min_idle_seconds: float) -> dict:
    db = Database(str(tmp_path / "idle-queue.db"))
    db.init_schema()
    quota = QuotaManager(db)
    usage_logs = UsageLogRepository(db)
    normal = FreeSmallDailyLimitManager(db)
    idle = IdleFreeSmallDailyLimitManager(db)
    user_id = _create_user(db)
    clock_values = [0.0]
    tracker = IdleFreeSmallTracker(
        occupancy_threshold_percent=50,
        min_idle_seconds=min_idle_seconds,
        clock=lambda: clock_values[0],
    )
    tracker.start()
    reservation = idle.try_reserve(user_id)
    usage_logs.insert_queued(
        UsageLogCreate(
            request_id="idle-success",
            user_id=user_id,
            action="generate",
            estimated_anlas_cost=0,
        )
    )
    usage_logs.insert_queued(
        UsageLogCreate(
            request_id="idle-rejected",
            user_id=user_id,
            action="generate",
            estimated_anlas_cost=0,
        )
    )
    usage_logs.insert_queued(
        UsageLogCreate(
            request_id="idle-cancel",
            user_id=user_id,
            action="generate",
            estimated_anlas_cost=0,
        )
    )
    usage_logs.insert_queued(
        UsageLogCreate(
            request_id="idle-retry",
            user_id=user_id,
            action="generate",
            estimated_anlas_cost=0,
        )
    )
    snapshot = normal.get_snapshot(user_id)
    return {
        "db": db,
        "quota": quota,
        "usage_logs": usage_logs,
        "idle": idle,
        "reservation": reservation,
        "context": IdleFreeSmallContext(daily_snapshot=snapshot, requested=1),
        "tracker": tracker,
        "user_id": user_id,
        "clock": clock_values,
    }


def _accounting(context: dict, request_id: str) -> RequestAccounting:
    return RequestAccounting(
        quota_manager=context["quota"],
        usage_logs=context["usage_logs"],
        request_id=request_id,
        user_id=context["user_id"],
        estimated_cost=0,
        idle_free_small_daily_limit_manager=context["idle"],
        idle_free_small_reservation=context["reservation"],
    )


def _create_user(db: Database) -> int:
    cursor = db.execute(
        "INSERT INTO users ("
        " api_key_hash, name, is_active, free_small_daily_limit_enabled,"
        " free_small_daily_limit, idle_free_small_multiplier, created_at"
        ") VALUES (?, ?, 1, 1, 1, 1, ?)",
        (f"queue-idle-{uuid.uuid4().hex}", "queue-idle-user", utc_now_iso()),
    )
    return int(cursor.lastrowid)
