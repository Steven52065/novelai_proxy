"""
测试脚本：验证延迟机制修改为"请求完成后延迟"

预期行为：
- 假设随机延迟固定为 5 秒
- 第一个请求耗时 20 秒
- 第二个请求应该在第一个请求完成后至少 5 秒才发送到上游
- 也就是说，第二个请求最早在第 25 秒时发送
"""
from __future__ import annotations

import asyncio
import time

import pytest


class MockProxyQueue:
    """简化的队列模拟，用于测试延迟逻辑"""

    def __init__(self, interval: float):
        self.interval = interval
        self._last_upstream_completed_at: float | None = None
        self.request_start_times: dict[str, float] = {}
        self.request_end_times: dict[str, float] = {}

    async def submit_request(self, request_id: str, upstream_duration: float) -> bytes:
        """提交请求"""
        # 等待延迟
        await self._wait_for_delay(request_id)

        # 记录请求开始时间
        self.request_start_times[request_id] = time.monotonic()

        # 执行上游请求
        await asyncio.sleep(upstream_duration)

        # 记录请求结束时间
        self.request_end_times[request_id] = time.monotonic()

        # 记录完成时间
        self._last_upstream_completed_at = time.monotonic()

        return b"result"

    async def _wait_for_delay(self, request_id: str):
        """等待延迟（新逻辑：基于完成时间）"""
        if self.interval <= 0:
            return

        if self._last_upstream_completed_at is None:
            # 首次请求，无需等待
            return

        elapsed = time.monotonic() - self._last_upstream_completed_at
        delay = self.interval - elapsed

        if delay > 0:
            await asyncio.sleep(delay)


@pytest.mark.asyncio
async def test_delay_after_completion():
    """测试请求完成后延迟

    验证第二个请求在第一个请求完成后至少等待指定的延迟时间
    """
    queue = MockProxyQueue(interval=0.5)  # 使用较短的延迟以加快测试
    start_time = time.monotonic()

    # 请求 A：耗时 0.2 秒
    await queue.submit_request("A", 0.2)
    time_after_a = time.monotonic() - start_time

    # 请求 B：耗时 0.1 秒
    await queue.submit_request("B", 0.1)
    time_after_b = time.monotonic() - start_time

    # 验证：请求 B 的总耗时应该是 0.5秒(延迟) + 0.1秒(执行) = 0.6秒
    actual_b_duration = time_after_b - time_after_a
    expected_b_duration = 0.5 + 0.1  # 延迟 + 执行时间

    # 允许 0.1 秒的误差（考虑系统调度延迟）
    assert abs(actual_b_duration - expected_b_duration) < 0.1, \
        f"Expected B duration ~{expected_b_duration}s, got {actual_b_duration:.3f}s"

    # 验证：请求 B 的开始时间应该在请求 A 完成后至少 0.5 秒
    time_between_completion_and_next_start = queue.request_start_times["B"] - queue.request_end_times["A"]
    assert time_between_completion_and_next_start >= 0.45, \
        f"Expected at least 0.5s delay after completion, got {time_between_completion_and_next_start:.3f}s"


@pytest.mark.asyncio
async def test_delay_with_queue_idle():
    """测试队列长时间空闲后无需等待

    验证如果上次请求完成后已经过了足够长的时间，新请求无需等待
    """
    queue = MockProxyQueue(interval=0.2)
    start_time = time.monotonic()

    # 请求 A：耗时 0.1 秒
    await queue.submit_request("A", 0.1)
    time_after_a = time.monotonic() - start_time

    # 模拟队列空闲 0.5 秒（远超过 0.2 秒的延迟要求）
    await asyncio.sleep(0.5)
    time_after_idle = time.monotonic() - start_time

    # 请求 B：耗时 0.1 秒
    await queue.submit_request("B", 0.1)
    time_after_b = time.monotonic() - start_time

    # 验证：请求 B 的耗时应该接近 0.1 秒（无需额外等待）
    actual_b_duration = time_after_b - time_after_idle
    expected_b_duration = 0.1  # 只有执行时间，无延迟

    assert abs(actual_b_duration - expected_b_duration) < 0.1, \
        f"Expected B duration ~{expected_b_duration}s (no delay), got {actual_b_duration:.3f}s"


@pytest.mark.asyncio
async def test_first_request_no_delay():
    """测试首次请求无需等待"""
    queue = MockProxyQueue(interval=0.5)
    start_time = time.monotonic()

    # 首次请求
    await queue.submit_request("A", 0.1)
    elapsed = time.monotonic() - start_time

    # 验证：首次请求的耗时应该接近 0.1 秒（无延迟）
    assert abs(elapsed - 0.1) < 0.1, \
        f"Expected first request to take ~0.1s (no delay), got {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_zero_interval_no_delay():
    """测试延迟为 0 时无需等待"""
    queue = MockProxyQueue(interval=0.0)

    # 连续发送两个请求
    await queue.submit_request("A", 0.05)

    start_time = time.monotonic()
    await queue.submit_request("B", 0.05)
    elapsed = time.monotonic() - start_time

    # 验证：延迟为 0 时，请求 B 应该立即执行
    assert elapsed < 0.1, \
        f"Expected immediate execution with 0 interval, got {elapsed:.3f}s"
