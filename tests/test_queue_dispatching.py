from __future__ import annotations

import asyncio
import threading

from helpers import PAYLOAD, BlockingFakeUpstream, FakeUpstream
from app.queue_manager import QueueFull, RoutingProxyQueue, UpstreamExecutionTimeout, UpstreamQueueTarget
from queue_manager_helpers import (
    CancellationResistantTimeoutUpstream,
    TimeoutByLabelUpstream,
    _NoopUsageLogs,
    _wait_until_async,
)


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
            await _wait_until_async(lambda: queue._queues["opus-a"].running_item is not None)

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

def test_upstream_execution_timeout_keeps_upstream_serial_until_handler_finishes():
    async def run_test():
        cleanup_event = asyncio.Event()
        upstream = CancellationResistantTimeoutUpstream(cleanup_event=cleanup_event)
        queue = RoutingProxyQueue(
            targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream)],
            quota_manager=object(),
            usage_logs=_NoopUsageLogs(),
            max_queue_size=2,
            upstream_interval_min_seconds=0,
            upstream_interval_max_seconds=0,
            upstream_error_extra_delay_seconds=0,
            upstream_execution_timeout_seconds=0.02,
        )
        queue.start()
        fast_task = None
        try:
            slow_task = asyncio.create_task(
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
                await asyncio.wait_for(slow_task, timeout=1)
            except UpstreamExecutionTimeout:
                pass
            else:
                raise AssertionError("expected upstream execution timeout")

            assert upstream.cancel_seen

            fast_task = asyncio.create_task(
                queue.submit(
                    request_id="fast-after-timeout",
                    user_id=1,
                    tier="normal",
                    action="generate",
                    logging_config=object(),
                    estimated_cost=0,
                    handler=lambda upstream: upstream.generate_image_payload_zip({"label": "fast"}),
                    process_zip_response=False,
                    manage_quota=False,
                )
            )
            await asyncio.sleep(0.05)

            assert upstream.started_labels == ["slow"]
            assert upstream.max_active == 1
            assert not fast_task.done()

            cleanup_event.set()
            assert await asyncio.wait_for(fast_task, timeout=1) == b"fast"
            assert upstream.started_labels == ["slow", "fast"]
            assert upstream.finished_labels == ["slow", "fast"]
            assert upstream.max_active == 1
        finally:
            cleanup_event.set()
            if fast_task is not None and not fast_task.done():
                fast_task.cancel()
            await queue.stop(drain=False)

    asyncio.run(run_test())

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
