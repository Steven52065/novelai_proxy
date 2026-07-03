from __future__ import annotations

import asyncio
import threading
import time

import pytest
from novelai_python._exceptions import APIError

from app.queue_manager import (
    NoAvailableUpstream,
    QueueFull,
    QueueItem,
    RoutingProxyQueue,
    UpstreamItemRerouted,
    UpstreamQueueTarget,
)
from app.request_accounting import RequestAccounting
from queue_manager_helpers import FirstRequestBlockingLabelUpstream, _NoopUsageLogs, _wait_until_async


class LabelUpstream:
    def __init__(self):
        self.started_labels = []

    async def generate_image_payload_zip(self, payload):
        label = payload["label"]
        self.started_labels.append(label)
        return label.encode("utf-8")


class RecordingRetryUsageLogs(_NoopUsageLogs):
    def __init__(self):
        self.retry_attempts = []
        self.rejected = []

    def insert_retry_attempt(self, *, request_id=None, attempt_number, upstream_id=None):
        self.retry_attempts.append(
            {
                "request_id": request_id,
                "attempt_number": attempt_number,
                "upstream_id": upstream_id,
            }
        )

    def mark_rejected(self, request_id, *, error_code, error_message, log_level="ERROR", attempt_number=0):
        self.rejected.append(
            {
                "request_id": request_id,
                "attempt_number": attempt_number,
                "error_code": error_code,
                "error_message": error_message,
                "log_level": log_level,
            }
        )


def _api_429() -> APIError:
    return APIError(
        "Too many requests",
        request={},
        response={"message": "Too many requests"},
        code="429",
    )


def test_disabled_upstream_pending_user_request_reroutes_to_enabled_target():
    async def run_test():
        release_a = threading.Event()
        upstream_a = FirstRequestBlockingLabelUpstream(release_a)
        upstream_b = LabelUpstream()
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream_a)],
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
                    request_id="running-on-a",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "running-on-a"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: upstream_a.started_labels == ["running-on-a"])

            second = asyncio.create_task(
                queue.submit(
                    request_id="rerouted-to-b",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "rerouted-to-b"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: queue._queues["opus-a"].qsize() == 1)

            queue.sync_targets([UpstreamQueueTarget(id="opus-b", client_provider=lambda: upstream_b)])

            assert "opus-a" not in queue._queues
            assert await asyncio.wait_for(second, timeout=1) == b"rerouted-to-b"
            assert upstream_b.started_labels == ["rerouted-to-b"]

            release_a.set()
            assert await asyncio.wait_for(first, timeout=1) == b"running-on-a"
            assert upstream_a.started_labels == ["running-on-a"]
        finally:
            release_a.set()
            await queue.stop()

    asyncio.run(run_test())


def test_removed_upstream_queue_worker_is_stopped():
    async def run_test():
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=LabelUpstream)],
            quota_manager=object(),
            usage_logs=_NoopUsageLogs(),
            max_queue_size=2,
            upstream_interval_min_seconds=0,
            upstream_interval_max_seconds=0,
            upstream_error_extra_delay_seconds=0,
        )
        queue.start()
        old_queue = queue._queues["opus-a"]
        assert old_queue._worker is not None

        queue.sync_targets([UpstreamQueueTarget(id="opus-b", client_provider=LabelUpstream)])

        assert "opus-a" not in queue._queues
        await _wait_until_async(lambda: old_queue._worker is not None and old_queue._worker.done())
        await queue.stop()

    asyncio.run(run_test())


def test_removed_upstream_inflight_request_completes_before_worker_stops():
    async def run_test():
        release_a = threading.Event()
        upstream_a = FirstRequestBlockingLabelUpstream(release_a)
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream_a)],
            quota_manager=object(),
            usage_logs=_NoopUsageLogs(),
            max_queue_size=2,
            upstream_interval_min_seconds=0,
            upstream_interval_max_seconds=0,
            upstream_error_extra_delay_seconds=0,
        )
        queue.start()
        old_queue = queue._queues["opus-a"]
        try:
            first = asyncio.create_task(
                queue.submit(
                    request_id="removed-inflight",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "removed-inflight"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: upstream_a.started_labels == ["removed-inflight"])

            queue.sync_targets([])

            assert "opus-a" not in queue._queues
            release_a.set()
            assert await asyncio.wait_for(first, timeout=1) == b"removed-inflight"
            await _wait_until_async(lambda: old_queue._worker is not None and old_queue._worker.done())
        finally:
            release_a.set()
            await queue.stop()

    asyncio.run(run_test())


def test_removed_upstream_can_be_readded_with_new_queue():
    async def run_test():
        upstream_a_old = LabelUpstream()
        upstream_a_new = LabelUpstream()
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream_a_old)],
            quota_manager=object(),
            usage_logs=_NoopUsageLogs(),
            max_queue_size=2,
            upstream_interval_min_seconds=0,
            upstream_interval_max_seconds=0,
            upstream_error_extra_delay_seconds=0,
        )
        queue.start()
        old_queue = queue._queues["opus-a"]
        try:
            queue.sync_targets([])
            await _wait_until_async(lambda: old_queue._worker is not None and old_queue._worker.done())

            queue.sync_targets([UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream_a_new)])

            assert queue._queues["opus-a"] is not old_queue
            result = await queue.submit(
                request_id="readded-upstream",
                user_id=1,
                tier="normal",
                action="generate",
                logging_config=object(),
                estimated_cost=0,
                handler=lambda upstream: upstream.generate_image_payload_zip({"label": "readded-upstream"}),
                process_zip_response=False,
                manage_quota=False,
            )
            assert result == b"readded-upstream"
            assert upstream_a_old.started_labels == []
            assert upstream_a_new.started_labels == ["readded-upstream"]
        finally:
            await queue.stop()

    asyncio.run(run_test())


def test_disabled_upstream_pending_user_request_fails_when_only_allowed_target_is_disabled():
    async def run_test():
        release_a = threading.Event()
        upstream_a = FirstRequestBlockingLabelUpstream(release_a)
        upstream_b = LabelUpstream()
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream_a)],
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
                    request_id="running-on-a",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "running-on-a"}),
                    process_zip_response=False,
                    manage_quota=False,
                    allowed_upstreams=frozenset({"opus-a"}),
                )
            )
            await _wait_until_async(lambda: upstream_a.started_labels == ["running-on-a"])

            second = asyncio.create_task(
                queue.submit(
                    request_id="only-a",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "only-a"}),
                    process_zip_response=False,
                    manage_quota=False,
                    allowed_upstreams=frozenset({"opus-a"}),
                )
            )
            await _wait_until_async(lambda: queue._queues["opus-a"].qsize() == 1)

            queue.sync_targets([UpstreamQueueTarget(id="opus-b", client_provider=lambda: upstream_b)])

            assert "opus-a" not in queue._queues
            with pytest.raises(NoAvailableUpstream):
                await asyncio.wait_for(second, timeout=1)
            assert upstream_b.started_labels == []

            release_a.set()
            assert await asyncio.wait_for(first, timeout=1) == b"running-on-a"
        finally:
            release_a.set()
            await queue.stop()

    asyncio.run(run_test())


def test_disabled_upstream_pending_user_request_fails_when_no_upstream_remains():
    async def run_test():
        release_a = threading.Event()
        upstream_a = FirstRequestBlockingLabelUpstream(release_a)
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream_a)],
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
                    request_id="running-on-a",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "running-on-a"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: upstream_a.started_labels == ["running-on-a"])

            second = asyncio.create_task(
                queue.submit(
                    request_id="no-upstream-left",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "no-upstream-left"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: queue._queues["opus-a"].qsize() == 1)

            queue.sync_targets([])

            assert "opus-a" not in queue._queues
            with pytest.raises(NoAvailableUpstream):
                await asyncio.wait_for(second, timeout=1)

            release_a.set()
            assert await asyncio.wait_for(first, timeout=1) == b"running-on-a"
        finally:
            release_a.set()
            await queue.stop()

    asyncio.run(run_test())


def test_disabled_upstream_pending_user_request_fails_when_dispatch_queue_is_full():
    async def run_test():
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=LabelUpstream)],
            quota_manager=object(),
            usage_logs=_NoopUsageLogs(),
            max_queue_size=2,
            dispatch_max_queue_size=1,
        )
        loop = asyncio.get_running_loop()
        original_future = loop.create_future()
        queued_item = QueueItem(
            priority=RoutingProxyQueue.NORMAL_PRIORITY,
            sequence=0,
            enqueued_at=time.monotonic(),
            request_id="dispatch-full-reroute",
            user_id=1,
            action="generate",
            tier="normal",
            estimated_cost=0,
            accounting=RequestAccounting(
                quota_manager=object(),
                usage_logs=_NoopUsageLogs(),
                request_id="dispatch-full-reroute",
                user_id=1,
                estimated_cost=0,
                manage_quota=False,
            ),
            logging_config=object(),
            process_zip_response=False,
            handler=lambda upstream: upstream.generate_image_payload_zip({"label": "dispatch-full-reroute"}),
            future=original_future,
        )
        attempt_future = queue._queues["opus-a"].enqueue(queued_item)

        filler_future = loop.create_future()
        filler_item = QueueItem(
            priority=RoutingProxyQueue.NORMAL_PRIORITY,
            sequence=1,
            enqueued_at=time.monotonic(),
            request_id="dispatch-filler",
            user_id=1,
            action="generate",
            tier="normal",
            estimated_cost=0,
            accounting=RequestAccounting(
                quota_manager=object(),
                usage_logs=_NoopUsageLogs(),
                request_id="dispatch-filler",
                user_id=1,
                estimated_cost=0,
                manage_quota=False,
            ),
            logging_config=object(),
            process_zip_response=False,
            handler=lambda upstream: upstream.generate_image_payload_zip({"label": "dispatch-filler"}),
            future=filler_future,
        )
        queue._dispatch_queue.put_nowait(filler_item)

        queue.sync_targets([UpstreamQueueTarget(id="opus-b", client_provider=LabelUpstream)])

        assert "opus-a" not in queue._queues
        with pytest.raises(QueueFull):
            await original_future
        with pytest.raises(UpstreamItemRerouted):
            await attempt_future

    asyncio.run(run_test())


def test_disabled_upstream_pending_admin_probe_fails_without_reroute():
    async def run_test():
        release_a = threading.Event()
        upstream_a = FirstRequestBlockingLabelUpstream(release_a)
        upstream_b = LabelUpstream()
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream_a)],
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
                    request_id="running-on-a",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "running-on-a"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: upstream_a.started_labels == ["running-on-a"])

            probe = asyncio.create_task(
                queue.submit_upstream_probe(
                    upstream_id="opus-a",
                    request_id="admin-probe",
                    logging_config=object(),
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "admin-probe"}),
                )
            )
            await _wait_until_async(lambda: queue._queues["opus-a"].qsize() == 1)

            queue.sync_targets([UpstreamQueueTarget(id="opus-b", client_provider=lambda: upstream_b)])

            assert "opus-a" not in queue._queues
            with pytest.raises(NoAvailableUpstream):
                await asyncio.wait_for(probe, timeout=1)
            assert upstream_b.started_labels == []

            release_a.set()
            assert await asyncio.wait_for(first, timeout=1) == b"running-on-a"
        finally:
            release_a.set()
            await queue.stop()

    asyncio.run(run_test())


def test_disabled_upstream_rerouted_retry_attempt_does_not_duplicate_retry_log():
    async def run_test():
        release_a = threading.Event()
        upstream_a = FirstRequestBlockingLabelUpstream(release_a)
        upstream_b = LabelUpstream()
        usage_logs = RecordingRetryUsageLogs()
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream_a)],
            quota_manager=object(),
            usage_logs=usage_logs,
            max_queue_size=2,
            upstream_interval_min_seconds=0,
            upstream_interval_max_seconds=0,
            upstream_error_extra_delay_seconds=0,
        )
        queue.start()
        try:
            first = asyncio.create_task(
                queue.submit(
                    request_id="running-on-a",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "running-on-a"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: upstream_a.started_labels == ["running-on-a"])

            loop = asyncio.get_running_loop()
            original_future = loop.create_future()
            accounting = RequestAccounting(
                quota_manager=object(),
                usage_logs=usage_logs,
                request_id="retry-reroute",
                user_id=7,
                estimated_cost=0,
                manage_quota=False,
            )
            retry_item = QueueItem(
                priority=RoutingProxyQueue.NORMAL_PRIORITY,
                sequence=0,
                enqueued_at=time.monotonic(),
                request_id="retry-reroute",
                user_id=7,
                action="generate",
                tier="normal",
                estimated_cost=0,
                accounting=accounting,
                logging_config=object(),
                process_zip_response=False,
                handler=lambda upstream: upstream.generate_image_payload_zip({"label": "retry-reroute"}),
                future=original_future,
                attempt_number=1,
                has_retried_429=True,
            )
            queue._dispatch_to_upstream(retry_item)
            await _wait_until_async(lambda: queue._queues["opus-a"].qsize() == 1)
            assert usage_logs.retry_attempts == [
                {"request_id": "retry-reroute", "attempt_number": 1, "upstream_id": "opus-a"}
            ]

            queue.sync_targets([UpstreamQueueTarget(id="opus-b", client_provider=lambda: upstream_b)])

            assert "opus-a" not in queue._queues
            assert await asyncio.wait_for(original_future, timeout=1) == b"retry-reroute"
            assert upstream_b.started_labels == ["retry-reroute"]
            assert usage_logs.retry_attempts == [
                {"request_id": "retry-reroute", "attempt_number": 1, "upstream_id": "opus-a"}
            ]

            release_a.set()
            assert await asyncio.wait_for(first, timeout=1) == b"running-on-a"
        finally:
            release_a.set()
            await queue.stop()

    asyncio.run(run_test())


def test_disabled_upstream_rerouted_retry_attempt_is_rejected_when_no_upstream_remains():
    async def run_test():
        release_a = threading.Event()
        upstream_a = FirstRequestBlockingLabelUpstream(release_a)
        usage_logs = RecordingRetryUsageLogs()
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream_a)],
            quota_manager=object(),
            usage_logs=usage_logs,
            max_queue_size=2,
            upstream_interval_min_seconds=0,
            upstream_interval_max_seconds=0,
            upstream_error_extra_delay_seconds=0,
        )
        queue.start()
        try:
            first = asyncio.create_task(
                queue.submit(
                    request_id="running-on-a",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "running-on-a"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await _wait_until_async(lambda: upstream_a.started_labels == ["running-on-a"])

            loop = asyncio.get_running_loop()
            original_future = loop.create_future()
            accounting = RequestAccounting(
                quota_manager=object(),
                usage_logs=usage_logs,
                request_id="retry-no-upstream",
                user_id=7,
                estimated_cost=0,
                manage_quota=False,
            )
            retry_item = QueueItem(
                priority=RoutingProxyQueue.NORMAL_PRIORITY,
                sequence=0,
                enqueued_at=time.monotonic(),
                request_id="retry-no-upstream",
                user_id=7,
                action="generate",
                tier="normal",
                estimated_cost=0,
                accounting=accounting,
                logging_config=object(),
                process_zip_response=False,
                handler=lambda upstream: upstream.generate_image_payload_zip({"label": "retry-no-upstream"}),
                future=original_future,
                attempt_number=1,
                has_retried_429=True,
                last_429_error=_api_429(),
            )
            queue._dispatch_to_upstream(retry_item)
            await _wait_until_async(lambda: queue._queues["opus-a"].qsize() == 1)

            queue.sync_targets([])

            assert "opus-a" not in queue._queues
            with pytest.raises(APIError):
                await asyncio.wait_for(original_future, timeout=1)
            assert usage_logs.retry_attempts == [
                {"request_id": "retry-no-upstream", "attempt_number": 1, "upstream_id": "opus-a"}
            ]
            assert usage_logs.rejected == [
                {
                    "request_id": "retry-no-upstream",
                    "attempt_number": 1,
                    "error_code": "no_available_upstream",
                    "error_message": "No enabled upstream is available for this user",
                    "log_level": "ERROR",
                }
            ]

            release_a.set()
            assert await asyncio.wait_for(first, timeout=1) == b"running-on-a"
        finally:
            release_a.set()
            await queue.stop()

    asyncio.run(run_test())


def test_disabled_upstream_rerouted_retry_attempt_is_rejected_when_dispatch_queue_is_full():
    async def run_test():
        usage_logs = RecordingRetryUsageLogs()
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=LabelUpstream)],
            quota_manager=object(),
            usage_logs=usage_logs,
            max_queue_size=2,
            dispatch_max_queue_size=1,
        )
        loop = asyncio.get_running_loop()
        original_future = loop.create_future()
        retry_item = QueueItem(
            priority=RoutingProxyQueue.NORMAL_PRIORITY,
            sequence=0,
            enqueued_at=time.monotonic(),
            request_id="retry-dispatch-full",
            user_id=7,
            action="generate",
            tier="normal",
            estimated_cost=0,
            accounting=RequestAccounting(
                quota_manager=object(),
                usage_logs=usage_logs,
                request_id="retry-dispatch-full",
                user_id=7,
                estimated_cost=0,
                manage_quota=False,
            ),
            logging_config=object(),
            process_zip_response=False,
            handler=lambda upstream: upstream.generate_image_payload_zip({"label": "retry-dispatch-full"}),
            future=original_future,
            attempt_number=1,
            has_retried_429=True,
            last_429_error=_api_429(),
        )
        attempt_future = queue._queues["opus-a"].enqueue(retry_item)

        filler_future = loop.create_future()
        filler_item = QueueItem(
            priority=RoutingProxyQueue.NORMAL_PRIORITY,
            sequence=1,
            enqueued_at=time.monotonic(),
            request_id="dispatch-filler",
            user_id=1,
            action="generate",
            tier="normal",
            estimated_cost=0,
            accounting=RequestAccounting(
                quota_manager=object(),
                usage_logs=_NoopUsageLogs(),
                request_id="dispatch-filler",
                user_id=1,
                estimated_cost=0,
                manage_quota=False,
            ),
            logging_config=object(),
            process_zip_response=False,
            handler=lambda upstream: upstream.generate_image_payload_zip({"label": "dispatch-filler"}),
            future=filler_future,
        )
        queue._dispatch_queue.put_nowait(filler_item)

        queue.sync_targets([UpstreamQueueTarget(id="opus-b", client_provider=LabelUpstream)])

        assert "opus-a" not in queue._queues
        with pytest.raises(QueueFull):
            await original_future
        with pytest.raises(UpstreamItemRerouted):
            await attempt_future
        assert usage_logs.retry_attempts == [
            {"request_id": "retry-dispatch-full", "attempt_number": 1, "upstream_id": "opus-a"}
        ]
        assert usage_logs.rejected == [
            {
                "request_id": "retry-dispatch-full",
                "attempt_number": 1,
                "error_code": "queue_full",
                "error_message": "Queue full, please retry later",
                "log_level": "ERROR",
            }
        ]

    asyncio.run(run_test())
