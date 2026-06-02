"""
测试脚本：验证延迟机制修改为"请求完成后延迟"

预期行为：
- 假设随机延迟固定为 5 秒
- 第一个请求耗时 20 秒
- 第二个请求应该在第一个请求完成后至少 5 秒才发送到上游
- 也就是说，第二个请求最早在第 25 秒时发送
"""
import asyncio
import time


async def simulate_upstream_request(request_id: str, duration: float) -> bytes:
    """模拟上游请求"""
    print(f"[{time.time():.2f}] 请求 {request_id} 开始发送到上游")
    await asyncio.sleep(duration)
    print(f"[{time.time():.2f}] 请求 {request_id} 完成，耗时 {duration} 秒")
    return b"result"


class MockProxyQueue:
    """简化的队列模拟，用于测试延迟逻辑"""

    def __init__(self, interval: float):
        self.interval = interval
        self._last_upstream_completed_at = None

    async def submit_request(self, request_id: str, upstream_duration: float):
        """提交请求"""
        # 等待延迟
        await self._wait_for_delay(request_id)

        # 执行上游请求
        result = await simulate_upstream_request(request_id, upstream_duration)

        # 记录完成时间
        self._last_upstream_completed_at = time.time()

        return result

    async def _wait_for_delay(self, request_id: str):
        """等待延迟（新逻辑：基于完成时间）"""
        if self.interval <= 0:
            return

        if self._last_upstream_completed_at is None:
            # 首次请求，无需等待
            print(f"[{time.time():.2f}] 请求 {request_id} 是首次请求，无需等待")
            return

        elapsed = time.time() - self._last_upstream_completed_at
        delay = self.interval - elapsed

        if delay > 0:
            print(f"[{time.time():.2f}] 请求 {request_id} 等待 {delay:.2f} 秒（上次完成后已过 {elapsed:.2f} 秒）")
            await asyncio.sleep(delay)
        else:
            print(f"[{time.time():.2f}] 请求 {request_id} 无需等待（上次完成后已过 {elapsed:.2f} 秒 > 要求的 {self.interval} 秒）")


async def test_delay_after_completion():
    """测试请求完成后延迟"""
    print("=" * 80)
    print("测试场景：请求完成后延迟")
    print("配置：固定延迟 5 秒")
    print("=" * 80)

    queue = MockProxyQueue(interval=5.0)
    start_time = time.time()

    # 请求 A：耗时 20 秒
    print(f"\n[{time.time() - start_time:.2f}] 提交请求 A（耗时 20 秒）")
    await queue.submit_request("A", 20.0)

    time_after_a = time.time() - start_time
    print(f"\n[{time_after_a:.2f}] 请求 A 完成，现在提交请求 B")

    # 请求 B：耗时 2 秒
    print(f"[{time.time() - start_time:.2f}] 提交请求 B（耗时 2 秒）")
    await queue.submit_request("B", 2.0)

    time_after_b = time.time() - start_time

    print("\n" + "=" * 80)
    print("测试结果：")
    print(f"  - 请求 A 完成时间: {time_after_a:.2f} 秒")
    print(f"  - 请求 B 开始时间: 应该在 {time_after_a + 5:.2f} 秒左右")
    print(f"  - 请求 B 完成时间: {time_after_b:.2f} 秒")
    print(f"  - 请求 B 实际等待: {time_after_b - time_after_a - 2:.2f} 秒（预期 5 秒）")

    expected_b_start = time_after_a + 5
    actual_b_duration = time_after_b - time_after_a

    if abs(actual_b_duration - 7.0) < 0.5:  # 5秒延迟 + 2秒执行 = 7秒
        print("[PASS] Test passed: Request B started 5 seconds after request A completed")
    else:
        print(f"[FAIL] Test failed: Expected 7 seconds, got {actual_b_duration:.2f} seconds")
    print("=" * 80)


async def test_delay_with_queue_idle():
    """测试队列空闲场景"""
    print("\n" + "=" * 80)
    print("测试场景：队列长时间空闲")
    print("配置：固定延迟 5 秒")
    print("=" * 80)

    queue = MockProxyQueue(interval=5.0)
    start_time = time.time()

    # 请求 A：耗时 2 秒
    print(f"\n[{time.time() - start_time:.2f}] 提交请求 A（耗时 2 秒）")
    await queue.submit_request("A", 2.0)

    time_after_a = time.time() - start_time
    print(f"\n[{time_after_a:.2f}] 请求 A 完成，等待 10 秒模拟队列空闲")

    # 模拟队列空闲 10 秒
    await asyncio.sleep(10.0)

    time_after_idle = time.time() - start_time
    print(f"\n[{time_after_idle:.2f}] 队列空闲 10 秒后，提交请求 B")

    # 请求 B：耗时 2 秒
    await queue.submit_request("B", 2.0)

    time_after_b = time.time() - start_time

    print("\n" + "=" * 80)
    print("测试结果：")
    print(f"  - 请求 A 完成时间: {time_after_a:.2f} 秒")
    print(f"  - 队列空闲后时间: {time_after_idle:.2f} 秒")
    print(f"  - 请求 B 完成时间: {time_after_b:.2f} 秒")
    print(f"  - 请求 B 实际等待: {time_after_b - time_after_idle - 2:.2f} 秒（预期 0 秒，因为已经空闲超过 5 秒）")

    if abs(time_after_b - time_after_idle - 2.0) < 0.5:  # 应该只有 2 秒执行时间
        print("✅ 测试通过：队列空闲超过延迟时间后，新请求无需等待")
    else:
        print(f"❌ 测试失败：预期无需等待，实际等待了 {time_after_b - time_after_idle - 2:.2f} 秒")
    print("=" * 80)


async def main():
    await test_delay_after_completion()
    await test_delay_with_queue_idle()


if __name__ == "__main__":
    asyncio.run(main())
