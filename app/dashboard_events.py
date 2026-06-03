from __future__ import annotations

import asyncio


class DashboardEventBus:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._version = 0

    @property
    def version(self) -> int:
        return self._version

    def notify_nowait(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.notify())

    async def notify(self) -> None:
        async with self._condition:
            self._version += 1
            self._condition.notify_all()

    async def wait_for_change(self, last_version: int, timeout: float) -> int:
        async with self._condition:
            if self._version != last_version:
                return self._version
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self._version != last_version),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                return last_version
            return self._version
