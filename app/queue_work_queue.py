from __future__ import annotations

import asyncio
import heapq
from collections import deque
from typing import Any


class PriorityWorkQueue:
    """Small priority queue with explicit snapshot and overflow support."""

    def __init__(self, *, maxsize: int = 0):
        self.maxsize = max(0, int(maxsize))
        self._items: list[Any] = []
        self._getters: deque[asyncio.Future] = deque()
        self._unfinished_tasks = 0
        self._finished = asyncio.Event()
        self._finished.set()

    def qsize(self) -> int:
        return len(self._items)

    def full(self) -> bool:
        return self.maxsize > 0 and self.qsize() >= self.maxsize

    def snapshot_items(self) -> list[Any]:
        return sorted(self._items)

    def put_nowait(self, item: Any, *, allow_overflow: bool = False) -> None:
        if self.full() and not allow_overflow:
            raise asyncio.QueueFull
        heapq.heappush(self._items, item)
        self._unfinished_tasks += 1
        self._finished.clear()
        self._wake_next_getter()

    async def get(self) -> Any:
        while not self._items:
            loop = asyncio.get_running_loop()
            getter = loop.create_future()
            self._getters.append(getter)
            try:
                await getter
            except BaseException:
                getter.cancel()
                try:
                    self._getters.remove(getter)
                except ValueError:
                    pass
                if self._items:
                    self._wake_next_getter()
                raise
        return heapq.heappop(self._items)

    def task_done(self) -> None:
        if self._unfinished_tasks <= 0:
            raise ValueError("task_done() called too many times")
        self._unfinished_tasks -= 1
        if self._unfinished_tasks == 0:
            self._finished.set()

    async def join(self) -> None:
        if self._unfinished_tasks > 0:
            await self._finished.wait()

    def _wake_next_getter(self) -> None:
        while self._getters:
            getter = self._getters.popleft()
            if not getter.done():
                getter.set_result(None)
                break
