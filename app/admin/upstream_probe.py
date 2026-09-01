from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..queue_errors import UpstreamExecutionTimeout


class UpstreamProbeBusy(Exception):
    """同一个上游已有一个队列外探测在执行。"""


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
        try:
            return await asyncio.wait_for(handler(client), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise UpstreamExecutionTimeout(timeout_seconds) from exc
        finally:
            self._in_flight.discard(upstream_id)
