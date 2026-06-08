from __future__ import annotations

import asyncio
import threading
import time

from helpers import PAYLOAD, FakeUpstream
from app.free_small_daily_limit import FreeSmallDailyReservation
from app.queue_manager import DispatchQueueItem, QueueClosed, Retry429Error, RoutingProxyQueue, UpstreamQueueTarget
from queue_manager_helpers import (
    FirstRequestBlockingLabelUpstream,
    One429ThenSuccessfulUpstream,
    _NoopUsageLogs,
    _RecordingQuota,
    _RecordingUsageLogs,
    _api_429,
    _wait_until_async,
)


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


def test_retry_attempt_insert_failure_releases_reserved_accounting():
    class FailingRetryUsageLogs(_NoopUsageLogs):
        def insert_retry_attempt(self, *args, **kwargs):
            raise RuntimeError("retry log insert failed")

    class RecordingDailyLimit:
        def __init__(self):
            self.released = []

        def release(self, reservation):
            self.released.append(reservation)

    async def run_test():
        release_first = threading.Event()
        upstream = One429ThenSuccessfulUpstream(release_first, fail_label="retry-log-fails")
        quota = _RecordingQuota()
        daily = RecordingDailyLimit()
        reservation = FreeSmallDailyReservation(
            user_id=42,
            window_start="2026-06-08T00:00:00+08:00",
            count=1,
            scope="user",
            limit=1,
            reset_at="2026-06-09T00:00:00+08:00",
        )
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream)],
            quota_manager=quota,
            usage_logs=FailingRetryUsageLogs(),
            max_queue_size=2,
            upstream_interval_min_seconds=0,
            upstream_interval_max_seconds=0,
            upstream_error_extra_delay_seconds=0,
        )
        queue.start()
        try:
            task = asyncio.create_task(
                queue.submit(
                    request_id="retry-log-fails",
                    user_id=42,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=7,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "retry-log-fails"}),
                    process_zip_response=False,
                    free_small_daily_limit_manager=daily,
                    free_small_daily_reservation=reservation,
                )
            )
            await _wait_until_async(lambda: upstream.started_labels == ["retry-log-fails"])
            release_first.set()

            try:
                await asyncio.wait_for(task, timeout=2)
            except RuntimeError as exc:
                assert str(exc) == "retry log insert failed"
            else:
                raise AssertionError("expected retry log insert failure")

            assert quota.released == [(42, 7)]
            assert daily.released == [reservation]
            assert upstream.started_labels == ["retry-log-fails"]
        finally:
            release_first.set()
            await queue.stop()

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
