
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from sqlite3 import OperationalError

import pytest

from conftest import wait_until_async
from app.api_errors import APIError
from app.config import LoggingConfig
from app.database import Database, utc_now_iso
from app.free_small_daily_limit import FreeSmallDailyLimitManager
from app.idle_free_small import IdleFreeSmallContext, IdleFreeSmallTracker
from app.idle_free_small_daily_limit import IdleFreeSmallDailyLimitManager
from app.queue_errors import IdleFreeSmallRejected, QueueClosed, Retry429Error
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


class Blocking429OnceUpstream(BlockingUpstream):
    async def generate_image_payload_zip(self, payload):
        self.calls += 1
        self.started.set()
        if self.calls == 1:
            await self.release.wait()
            raise APIError("Too many requests", code="429", request=payload, response={})
        return b"retry-success"


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


@pytest.mark.parametrize("tier", ["normal", "vip"])
@pytest.mark.parametrize("force_stop", [False, True])
def test_reenabled_upstream_serializes_all_workers_in_an_idle_pool(tmp_path: Path, tier, force_stop):
    async def run_test():
        context = _idle_context(tmp_path, min_idle_seconds=30)
        upstream = BlockingUpstream()
        other = ImmediateUpstream()
        target_a = UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream)
        target_b = UpstreamQueueTarget(id="opus-b", client_provider=lambda: other)
        target_c = UpstreamQueueTarget(id="opus-c", client_provider=lambda: other)
        queue = RoutingProxyQueue(
            targets=[target_a, target_b, target_c],
            quota_manager=context["quota"],
            usage_logs=context["usage_logs"],
            max_queue_size=5,
            upstream_interval_min_seconds=0,
            upstream_interval_max_seconds=0,
            upstream_error_extra_delay_seconds=0,
            tracker=context["tracker"],
        )
        queue.start()
        futures = []
        workers = []

        def enqueue_normal(request_id):
            context["usage_logs"].insert_queued(
                UsageLogCreate(request_id=request_id, user_id=context["user_id"], action="generate", estimated_anlas_cost=0)
            )
            return queue.enqueue(
                request_id=request_id, user_id=context["user_id"], tier=tier, action="generate",
                logging_config=LoggingConfig(), estimated_cost=0, process_zip_response=False, manage_quota=False,
                handler=lambda upstream: upstream.generate_image_payload_zip({}),
            )

        try:
            futures.append(enqueue_normal("old-generation"))
            await asyncio.wait_for(upstream.started.wait(), timeout=1)
            workers.append(queue._queues["opus-a"])
            context["clock"][0] = 31
            assert queue.is_idle_free_available()

            # 保持轮询选中 A；B/C 让 1/3 占用仍满足空闲准入，不能靠阈值阻止同账号并发。
            for generation, targets in enumerate(([target_b, target_a, target_c], [target_b, target_c, target_a]), start=1):
                queue.sync_targets([target_b, target_c])
                queue.sync_targets(targets)
                if generation == 1:
                    futures.append(_enqueue_idle(queue, context, tier=tier))
                else:
                    futures.append(enqueue_normal("newest-generation"))
                worker = queue._queues["opus-a"]
                workers.append(worker)
                await wait_until_async(lambda: worker.running_item is not None)
                # 另一个上游仍可执行，也让已经派发的 handler 有机会运行。
                assert await asyncio.wait_for(
                    queue.submit_upstream_probe(
                        upstream_id="opus-b", request_id=f"other-{generation}", logging_config=LoggingConfig(),
                        handler=lambda upstream: upstream.generate_image_payload_zip({}),
                    ),
                    timeout=1,
                ) == b"idle-image"
                assert upstream.calls == 1
                assert all(not future.done() for future in futures)

            if force_stop:
                await asyncio.wait_for(queue.stop(drain=False), timeout=1)
                assert all(future.cancelled() for future in futures)
            else:
                upstream.release.set()
                assert await asyncio.wait_for(asyncio.gather(*futures), timeout=1) == [b"blocked-image"] * 3
                assert upstream.calls == 3
                await asyncio.wait_for(queue.stop(), timeout=1)

            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert (snapshot.used, snapshot.reserved) == (0 if force_stop else 1, 0)
            assert not queue._active_futures
            assert not context["tracker"].running_sources
            for worker in workers:
                await asyncio.wait_for(worker.queue.join(), timeout=1)
        finally:
            upstream.release.set()
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)
            await asyncio.gather(*futures, return_exceptions=True)
            context["db"].close()

    asyncio.run(run_test())


@pytest.mark.parametrize("tier", ["normal", "vip"])
@pytest.mark.parametrize("completion_yields", [0, 2])
def test_force_stop_blocks_429_retry_and_finishes_all_futures(tmp_path: Path, monkeypatch, tier, completion_yields):
    async def run_test():
        context = _idle_context(tmp_path, min_idle_seconds=0)
        upstream = Blocking429OnceUpstream()
        queue = _proxy_queue(context, upstream, tracker=context["tracker"])
        completion = queue._handle_upstream_completion
        retry_during_stop = []

        def record_completion(completed, **kwargs):
            if not completed.cancelled() and isinstance(completed.exception(), Retry429Error):
                retry_during_stop.append(not queue.accepting)
            return completion(completed, **kwargs)

        monkeypatch.setattr(queue, "_handle_upstream_completion", record_completion)
        queue.start()
        future = _enqueue_idle(queue, context, tier=tier)
        try:
            await asyncio.wait_for(upstream.started.wait(), timeout=1)
            upstream.release.set()
            for _ in range(completion_yields):
                await asyncio.sleep(0)
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)

            assert retry_during_stop == [True]
            assert future.done()
            assert isinstance(future.exception(), QueueClosed)
            assert upstream.calls == 1
            assert queue._dispatch_queue.qsize() == 0
            assert queue._queues["opus-a"].qsize() == 0
            assert not queue._active_futures
            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert (snapshot.used, snapshot.reserved) == (0, 0)
            rows = context["db"].query_all(
                "SELECT attempt_number, status, error_code FROM usage_logs WHERE request_id = ?", ("idle-success",)
            )
            assert [tuple(row) for row in rows] == [(0, "failed", "429")]
            await asyncio.wait_for(queue._dispatch_queue.join(), timeout=1)
            await asyncio.wait_for(queue._queues["opus-a"].queue.join(), timeout=1)
        finally:
            upstream.release.set()
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)
            await asyncio.gather(future, return_exceptions=True)
            context["db"].close()

    asyncio.run(run_test())


@pytest.mark.parametrize("tier", ["normal", "vip"])
def test_graceful_stop_allows_accepted_idle_request_to_retry(tmp_path: Path, tier):
    async def run_test():
        context = _idle_context(tmp_path, min_idle_seconds=0)
        upstream = Blocking429OnceUpstream()
        queue = _proxy_queue(context, upstream, tracker=context["tracker"])
        queue.start()
        future = _enqueue_idle(queue, context, tier=tier)
        stop_task = None
        try:
            await asyncio.wait_for(upstream.started.wait(), timeout=1)
            stop_task = asyncio.create_task(queue.stop())
            await wait_until_async(lambda: not queue.accepting)
            assert not stop_task.done()
            upstream.release.set()
            assert await asyncio.wait_for(future, timeout=1) == b"retry-success"
            await asyncio.wait_for(stop_task, timeout=1)
            assert upstream.calls == 2
            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert (snapshot.used, snapshot.reserved) == (1, 0)
            rows = context["db"].query_all(
                "SELECT attempt_number, status FROM usage_logs WHERE request_id = ? ORDER BY attempt_number", ("idle-success",)
            )
            assert [tuple(row) for row in rows] == [(0, "failed"), (1, "success")]
        finally:
            upstream.release.set()
            if stop_task is not None:
                await asyncio.wait_for(stop_task, timeout=1)
            else:
                await asyncio.wait_for(queue.stop(), timeout=1)
            context["db"].close()

    asyncio.run(run_test())


@pytest.mark.parametrize("phase", ["dispatch", "serial", "running"])
def test_force_stop_releases_idle_reservations_at_each_queue_stage(tmp_path: Path, phase):
    async def run_test():
        context = _idle_context(tmp_path, min_idle_seconds=0)
        upstream = BlockingUpstream()
        queue = _proxy_queue(context, upstream, tracker=context["tracker"])
        normal_future = None
        if phase != "dispatch":
            queue.start()
        if phase == "serial":
            normal_future = queue.enqueue(
                request_id="running-normal", user_id=context["user_id"], tier="normal", action="generate",
                logging_config=LoggingConfig(), estimated_cost=0, process_zip_response=False, manage_quota=False,
                handler=lambda upstream: upstream.generate_image_payload_zip({}),
            )
            await asyncio.wait_for(upstream.started.wait(), timeout=1)
        future = _enqueue_idle(queue, context)
        try:
            if phase == "serial":
                await wait_until_async(lambda: queue._queues["opus-a"].qsize() == 1)
            elif phase == "running":
                await asyncio.wait_for(upstream.started.wait(), timeout=1)
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)
            assert future.done()
            if phase == "running":
                assert future.cancelled()
            else:
                assert isinstance(future.exception(), QueueClosed)
            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert (snapshot.used, snapshot.reserved) == (0, 0)
            log = context["usage_logs"].get_by_request_id("idle-success")
            assert log["status"] == ("failed" if phase == "running" else "rejected")
            assert log["error_code"] == "server_shutting_down"
            assert not queue._active_futures
            assert not context["tracker"].running_sources
            await asyncio.wait_for(queue._dispatch_queue.join(), timeout=1)
            await asyncio.wait_for(queue._queues["opus-a"].queue.join(), timeout=1)
        finally:
            upstream.release.set()
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)
            await asyncio.gather(future, return_exceptions=True)
            if normal_future is not None:
                await asyncio.gather(normal_future, return_exceptions=True)
            context["db"].close()

    asyncio.run(run_test())


def test_force_stop_cancels_removed_worker_that_was_draining(tmp_path: Path):
    async def run_test():
        context = _idle_context(tmp_path, min_idle_seconds=0)
        upstream = BlockingUpstream()
        queue = _proxy_queue(context, upstream, tracker=context["tracker"])
        queue.start()
        future = _enqueue_idle(queue, context)
        try:
            await asyncio.wait_for(upstream.started.wait(), timeout=1)
            worker = queue._queues["opus-a"]
            queue.sync_targets([])
            await asyncio.sleep(0)
            assert worker.running_item is not None
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)

            assert future.cancelled()
            assert worker._worker.done()
            assert not upstream.release.is_set()
            assert not queue._removed_queues
            assert not context["tracker"].running_sources
            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert (snapshot.used, snapshot.reserved) == (0, 0)
            await asyncio.wait_for(worker.queue.join(), timeout=1)
        finally:
            upstream.release.set()
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)
            await asyncio.gather(future, return_exceptions=True)
            context["db"].close()

    asyncio.run(run_test())


def test_force_stop_also_releases_running_paid_request(tmp_path: Path):
    async def run_test():
        context = _idle_context(tmp_path, min_idle_seconds=0)
        context["idle"].release(context["reservation"])
        context["quota"].create_or_update(context["user_id"], total=100)
        context["quota"].reserve(context["user_id"], 7)
        context["usage_logs"].insert_queued(
            UsageLogCreate(request_id="paid-stop", user_id=context["user_id"], action="generate", estimated_anlas_cost=7)
        )
        upstream = BlockingUpstream()
        queue = _proxy_queue(context, upstream, tracker=context["tracker"])
        queue.start()
        future = queue.enqueue(
            request_id="paid-stop", user_id=context["user_id"], tier="normal", action="generate",
            logging_config=LoggingConfig(), estimated_cost=7, process_zip_response=False,
            handler=lambda upstream: upstream.generate_image_payload_zip({}),
        )
        try:
            await asyncio.wait_for(upstream.started.wait(), timeout=1)
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)
            assert future.cancelled()
            quota = context["quota"].get_snapshot(context["user_id"])
            assert (quota.used, quota.reserved) == (0, 0)
            log = context["usage_logs"].get_by_request_id("paid-stop")
            assert log["status"] == "failed"
            assert log["error_code"] == "server_shutting_down"
        finally:
            upstream.release.set()
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)
            context["db"].close()

    asyncio.run(run_test())


def test_caller_cancel_after_http_still_confirms_successful_idle_request(tmp_path: Path):
    async def run_test():
        context = _idle_context(tmp_path, min_idle_seconds=0)
        upstream = BlockingUpstream()
        queue = _proxy_queue(context, upstream, tracker=context["tracker"])
        queue.start()
        future = _enqueue_idle(queue, context)
        try:
            await asyncio.wait_for(upstream.started.wait(), timeout=1)
            future.cancel()
            upstream.release.set()
            await asyncio.wait_for(queue.stop(), timeout=1)
            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert (snapshot.used, snapshot.reserved) == (1, 0)
            assert context["usage_logs"].get_by_request_id("idle-success")["status"] == "success"
            assert upstream.calls == 1
        finally:
            upstream.release.set()
            await asyncio.wait_for(queue.stop(drain=False), timeout=1)
            context["db"].close()

    asyncio.run(run_test())


@pytest.mark.parametrize("tier", ["normal", "vip"])
def test_rerouted_retry_creates_log_for_next_attempt_and_confirms_once(tmp_path: Path, tier):
    async def run_test():
        context = _idle_context(tmp_path, min_idle_seconds=0)
        upstream_a = Retry429OnceUpstream()
        upstream_b = BlockingUpstream()
        upstream_c = ImmediateUpstream()
        target_a = UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream_a)
        target_b = UpstreamQueueTarget(id="opus-b", client_provider=lambda: upstream_b)
        target_c = UpstreamQueueTarget(id="opus-c", client_provider=lambda: upstream_c)
        queue = RoutingProxyQueue(
            targets=[target_a, target_b, target_c],
            quota_manager=context["quota"],
            usage_logs=context["usage_logs"],
            max_queue_size=2,
            upstream_interval_min_seconds=0,
            upstream_interval_max_seconds=0,
            upstream_error_extra_delay_seconds=0,
            tracker=context["tracker"],
        )
        queue.start()
        probe = asyncio.create_task(queue.submit_upstream_probe(
            upstream_id="opus-b", request_id="blocking-probe", logging_config=LoggingConfig(),
            handler=lambda upstream: upstream.generate_image_payload_zip({}),
        ))
        future = None
        try:
            await asyncio.wait_for(upstream_b.started.wait(), timeout=1)
            future = _enqueue_idle(queue, context, tier=tier)
            await wait_until_async(lambda: queue._queues["opus-b"].qsize() == 1)
            waiting_retry = queue._queues["opus-b"].queue.snapshot_items()[0]
            assert waiting_retry.attempt_number == 1
            assert waiting_retry.retry_attempt_logged is True

            queue.sync_targets([target_a, target_c])
            assert await asyncio.wait_for(future, timeout=1) == b"idle-image"
            assert (upstream_a.calls, upstream_b.calls, upstream_c.calls) == (2, 1, 1)
            rows = context["db"].query_all(
                "SELECT attempt_number, status, upstream_id, is_retry_success "
                "FROM usage_logs WHERE request_id = ? ORDER BY attempt_number", ("idle-success",)
            )
            assert [tuple(row) for row in rows] == [
                (0, "failed", "opus-a", 0),
                (1, "failed", "opus-a", 0),
                (2, "success", "opus-c", 1),
            ]
            snapshot = context["idle"].get_snapshot(context["user_id"])
            assert (snapshot.used, snapshot.reserved) == (1, 0)
        finally:
            upstream_b.release.set()
            await asyncio.wait_for(probe, timeout=1)
            await asyncio.wait_for(queue.stop(), timeout=1)
            if future is not None:
                await asyncio.gather(future, return_exceptions=True)
            context["db"].close()

    asyncio.run(run_test())


def _enqueue_idle(queue, context, request_id="idle-success", *, tier="normal"):
    return queue.enqueue(
        request_id=request_id,
        user_id=context["user_id"],
        tier=tier,
        action="generate",
        logging_config=LoggingConfig(),
        estimated_cost=0,
        handler=lambda upstream: upstream.generate_image_payload_zip({}),
        process_zip_response=False,
        accounting=_accounting(context, request_id),
        idle_free_small=context["context"],
    )


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
