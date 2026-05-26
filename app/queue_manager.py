from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from novelai_python._exceptions import APIError

from .database import Database, utc_now_iso
from .quota_manager import QuotaManager


@dataclass(order=True)
class QueueItem:
    priority: int
    sequence: int
    enqueued_at: float = field(compare=False)
    request_id: str = field(compare=False)
    user_id: int = field(compare=False)
    estimated_cost: int = field(compare=False)
    handler: Callable[[], Awaitable[bytes]] = field(compare=False)
    future: asyncio.Future = field(compare=False)


class ProxyQueue:
    def __init__(
        self,
        db: Database,
        quota_manager: QuotaManager,
        max_queue_size: int,
    ):
        self.db = db
        self.quota_manager = quota_manager
        self.queue: asyncio.PriorityQueue[QueueItem] = asyncio.PriorityQueue(maxsize=max_queue_size)
        self._sequence = itertools.count()
        self._worker: asyncio.Task | None = None

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass

    def qsize(self) -> int:
        return self.queue.qsize()

    async def submit(
        self,
        *,
        request_id: str,
        user_id: int,
        tier: str,
        estimated_cost: int,
        handler: Callable[[], Awaitable[bytes]],
    ) -> bytes:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        priority = 0 if tier == "vip" else 10
        item = QueueItem(
            priority=priority,
            sequence=next(self._sequence),
            enqueued_at=time.monotonic(),
            request_id=request_id,
            user_id=user_id,
            estimated_cost=estimated_cost,
            handler=handler,
            future=future,
        )
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull as exc:
            raise QueueFull from exc
        return await future

    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            queued_ms = int((time.monotonic() - item.enqueued_at) * 1000)
            try:
                self.db.execute(
                    """
                    UPDATE usage_logs
                    SET status = 'running', queued_ms = ?
                    WHERE request_id = ?
                    """,
                    (queued_ms, item.request_id),
                )
                payload = await item.handler()
            except Exception as exc:
                self.quota_manager.release(item.user_id, item.estimated_cost)
                code, message = self._error_details(exc)
                self.db.execute(
                    """
                    UPDATE usage_logs
                    SET status = 'failed',
                        queued_ms = COALESCE(queued_ms, ?),
                        error_code = ?,
                        error_message = ?,
                        completed_at = ?
                    WHERE request_id = ?
                    """,
                    (queued_ms, code, message[:500], utc_now_iso(), item.request_id),
                )
                if not item.future.done():
                    item.future.set_exception(exc)
            else:
                self.quota_manager.confirm(item.user_id, item.estimated_cost)
                self.db.execute(
                    """
                    UPDATE usage_logs
                    SET status = 'success',
                        queued_ms = COALESCE(queued_ms, ?),
                        final_anlas_cost = ?,
                        completed_at = ?
                    WHERE request_id = ?
                    """,
                    (queued_ms, item.estimated_cost, utc_now_iso(), item.request_id),
                )
                if not item.future.done():
                    item.future.set_result(payload)
            finally:
                self.queue.task_done()

    @staticmethod
    def _error_details(exc: Exception) -> tuple[str, str]:
        if isinstance(exc, APIError):
            return str(exc.code or "upstream_error"), exc.message
        return exc.__class__.__name__, str(exc)


class QueueFull(Exception):
    pass
