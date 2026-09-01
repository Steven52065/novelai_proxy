from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..queue_errors import UpstreamExecutionTimeout


class UpstreamProbeBusy(Exception):
    """同一个上游已有一个队列外探测在执行。"""


def _consume_handler_result(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


class DirectUpstreamProbe:
    """为不在调度队列里的上游（已禁用/自动禁用）执行队列外探测，单上游串行。"""

    def __init__(self) -> None:
        self._in_flight: set[str] = set()

    async def run(
        self,
        *,
        upstream_id: str,
        client: Any,
        handler: Callable[[Any], Awaitable[bytes]],
        timeout_seconds: float,
    ) -> bytes:
        if upstream_id in self._in_flight:
            raise UpstreamProbeBusy("该上游正在测试中，请稍后重试")
        self._in_flight.add(upstream_id)
        # 这里不用 asyncio.wait_for：它超时后会就地等待被取消的协程退栈完毕，
        # 而 handler 可能吞掉取消或卡在不可中断的调用上（参见 upstream_queue
        # 放弃等待僵尸 handler 的同样理由）。那样本次请求没有时间上限，
        # _in_flight 也永远清不掉，该上游此后只会返回 409。
        handler_task = asyncio.create_task(handler(client))
        try:
            done, _pending = await asyncio.wait({handler_task}, timeout=timeout_seconds)
            if handler_task in done:
                return handler_task.result()
            handler_task.cancel()
            raise UpstreamExecutionTimeout(timeout_seconds, handler_task=handler_task)
        except asyncio.CancelledError:
            handler_task.cancel()
            raise
        finally:
            if not handler_task.done():
                handler_task.add_done_callback(_consume_handler_result)
            self._in_flight.discard(upstream_id)
