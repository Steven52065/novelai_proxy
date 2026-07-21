from __future__ import annotations

import asyncio

from helpers import FakeUpstream
from app.queue_errors import UpstreamExecutionTimeout
from app.queue_models import UpstreamQueueTarget
from app.routing_queue import RoutingProxyQueue
from queue_manager_helpers import (
    CancellationResistantTimeoutUpstream,
    _NoopUsageLogs,
)


def _make_queue(upstream, usage_logs=None, *, on_change=None, **overrides):
    kwargs = dict(
        targets=[UpstreamQueueTarget(id="opus-a", client_provider=lambda: upstream)],
        quota_manager=object(),
        usage_logs=usage_logs or _NoopUsageLogs(),
        max_queue_size=5,
        upstream_interval_min_seconds=0,
        upstream_interval_max_seconds=0,
        upstream_error_extra_delay_seconds=0,
        on_change=on_change,
    )
    kwargs.update(overrides)
    return RoutingProxyQueue(**kwargs)


def _submit(queue, request_id: str, label: str = "x"):
    return queue.submit(
        request_id=request_id,
        user_id=1,
        tier="normal",
        action="generate",
        logging_config=object(),
        estimated_cost=0,
        handler=lambda upstream: upstream.generate_image_payload_zip({"label": label}),
        process_zip_response=False,
        manage_quota=False,
    )


class _CrashOnSuccessUsageLogs(_NoopUsageLogs):
    def __init__(self):
        self.mark_success_calls = 0

    def mark_success(self, *args, **kwargs):
        self.mark_success_calls += 1
        raise RuntimeError("db down during mark_success")


class _CrashOnFailureUsageLogs(_NoopUsageLogs):
    def __init__(self):
        self.mark_failed_calls = 0

    def mark_failed(self, *args, **kwargs):
        self.mark_failed_calls += 1
        raise RuntimeError("db down during mark_failed")


class _FailingHandlerUpstream(FakeUpstream):
    def __init__(self, *, fail_label: str):
        super().__init__()
        self.fail_label = fail_label

    async def generate_image_payload_zip(self, payload):
        if payload["label"] == self.fail_label:
            raise RuntimeError("upstream boom")
        return payload["label"].encode("utf-8")


class _LabelUpstream(FakeUpstream):
    async def generate_image_payload_zip(self, payload):
        return payload["label"].encode("utf-8")


def test_worker_survives_success_settlement_crash():
    async def run_test():
        usage_logs = _CrashOnSuccessUsageLogs()
        queue = _make_queue(_LabelUpstream(), usage_logs)
        queue.start()
        try:
            first = await asyncio.wait_for(_submit(queue, "first", "first"), timeout=2)
            assert first == b"first"
            second = await asyncio.wait_for(_submit(queue, "second", "second"), timeout=2)
            assert second == b"second"
            assert usage_logs.mark_success_calls == 2
        finally:
            await queue.stop()

    asyncio.run(run_test())


def test_worker_survives_failure_settlement_crash():
    async def run_test():
        usage_logs = _CrashOnFailureUsageLogs()
        queue = _make_queue(_FailingHandlerUpstream(fail_label="bad"), usage_logs)
        queue.start()
        try:
            try:
                await asyncio.wait_for(_submit(queue, "bad", "bad"), timeout=2)
            except RuntimeError as exc:
                assert "upstream boom" in str(exc)
            else:
                raise AssertionError("expected the original upstream error")
            assert usage_logs.mark_failed_calls == 1

            after = await asyncio.wait_for(_submit(queue, "after", "after"), timeout=2)
            assert after == b"after"
        finally:
            await queue.stop()

    asyncio.run(run_test())


def test_worker_restarts_after_unexpected_death():
    async def run_test():
        queue = _make_queue(_LabelUpstream())
        upstream_queue = queue._queues["opus-a"]
        real_get = upstream_queue.queue.get
        state = {"crashed": False}

        async def flaky_get():
            if not state["crashed"]:
                state["crashed"] = True
                raise RuntimeError("worker loop crash")
            return await real_get()

        upstream_queue.queue.get = flaky_get
        queue.start()
        try:
            result = await asyncio.wait_for(_submit(queue, "revived", "revived"), timeout=2)
            assert result == b"revived"
            assert state["crashed"]
        finally:
            await queue.stop()

    asyncio.run(run_test())


def test_dispatcher_restarts_after_unexpected_death():
    async def run_test():
        queue = _make_queue(_LabelUpstream())
        real_get = queue._dispatch_queue.get
        state = {"crashed": False}

        async def flaky_get():
            if not state["crashed"]:
                state["crashed"] = True
                raise RuntimeError("dispatcher loop crash")
            return await real_get()

        queue._dispatch_queue.get = flaky_get
        queue.start()
        try:
            result = await asyncio.wait_for(_submit(queue, "revived", "revived"), timeout=2)
            assert result == b"revived"
            assert state["crashed"]
        finally:
            await queue.stop()

    asyncio.run(run_test())


def test_timed_out_handler_cleanup_grace_frees_queue():
    async def run_test():
        cleanup_event = asyncio.Event()
        upstream = CancellationResistantTimeoutUpstream(cleanup_event=cleanup_event)
        queue = _make_queue(
            upstream,
            upstream_execution_timeout_seconds=0.02,
            upstream_timeout_cleanup_grace_seconds=0.1,
        )
        queue.start()
        fast_task = None
        try:
            slow_task = asyncio.create_task(_submit(queue, "slow-timeout", "slow"))
            try:
                await asyncio.wait_for(slow_task, timeout=1)
            except UpstreamExecutionTimeout:
                pass
            else:
                raise AssertionError("expected upstream execution timeout")
            assert upstream.cancel_seen

            # handler 吞掉了取消且永不退出；宽限到期后队列必须继续消费。
            fast_task = asyncio.create_task(_submit(queue, "fast-after-grace", "fast"))
            assert await asyncio.wait_for(fast_task, timeout=2) == b"fast"
            assert upstream.started_labels == ["slow", "fast"]
        finally:
            cleanup_event.set()
            if fast_task is not None and not fast_task.done():
                fast_task.cancel()
            await queue.stop(drain=False)

    asyncio.run(run_test())


def test_worker_and_dispatcher_survive_on_change_callback_crash():
    async def run_test():
        def bad_on_change():
            raise ZeroDivisionError("broken dashboard callback")

        queue = _make_queue(_LabelUpstream(), on_change=bad_on_change)
        queue.start()
        try:
            first = await asyncio.wait_for(_submit(queue, "first", "first"), timeout=2)
            assert first == b"first"
            second = await asyncio.wait_for(_submit(queue, "second", "second"), timeout=2)
            assert second == b"second"
        finally:
            await queue.stop()

    asyncio.run(run_test())
