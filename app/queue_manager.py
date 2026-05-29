from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from novelai_python._exceptions import APIError

from .config import LoggingConfig
from .database import Database, utc_now_iso
from .logging_utils import archive_zip_images, json_dumps, logger
from .quota_manager import QuotaManager


@dataclass(order=True)
class QueueItem:
    priority: int
    sequence: int
    enqueued_at: float = field(compare=False)
    request_id: str = field(compare=False)
    user_id: int = field(compare=False)
    action: str = field(compare=False)
    tier: str = field(compare=False)
    estimated_cost: int = field(compare=False)
    manage_quota: bool = field(compare=False)
    logging_config: LoggingConfig = field(compare=False)
    process_zip_response: bool = field(compare=False)
    handler: Callable[[], Awaitable[bytes]] = field(compare=False)
    future: asyncio.Future = field(compare=False)


class ProxyQueue:
    def __init__(
        self,
        db: Database,
        quota_manager: QuotaManager,
        max_queue_size: int,
        min_upstream_interval_seconds: float = 0,
    ):
        self.db = db
        self.quota_manager = quota_manager
        self.queue: asyncio.PriorityQueue[QueueItem] = asyncio.PriorityQueue(maxsize=max_queue_size)
        self.min_upstream_interval_seconds = max(0.0, float(min_upstream_interval_seconds))
        self._last_upstream_started_at: float | None = None
        self._sequence = itertools.count()
        self._worker: asyncio.Task | None = None
        self._running_item: QueueItem | None = None
        self._running_started_at: float | None = None

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

    def snapshot(self) -> dict[str, object]:
        now = time.monotonic()
        queued = [
            self._item_snapshot(item, now, "queued", position=index)
            for index, item in enumerate(sorted(self.queue._queue), start=1)
        ]
        running = None
        if self._running_item is not None:
            running = self._item_snapshot(self._running_item, now, "running", position=0)
            if self._running_started_at is not None:
                running["running_seconds"] = max(0, int(now - self._running_started_at))
        return {
            "queue_size": len(queued),
            "running": running,
            "queued": queued,
        }

    async def submit(
        self,
        *,
        request_id: str,
        user_id: int,
        tier: str,
        action: str,
        logging_config: LoggingConfig,
        estimated_cost: int,
        handler: Callable[[], Awaitable[bytes]],
        process_zip_response: bool = True,
        priority_override: int | None = None,
        manage_quota: bool = True,
    ) -> bytes:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        priority = priority_override if priority_override is not None else 0 if tier == "vip" else 10
        item = QueueItem(
            priority=priority,
            sequence=next(self._sequence),
            enqueued_at=time.monotonic(),
            request_id=request_id,
            user_id=user_id,
            action=action,
            tier=tier,
            estimated_cost=estimated_cost,
            manage_quota=manage_quota,
            logging_config=logging_config,
            process_zip_response=process_zip_response,
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
            self._running_item = item
            self._running_started_at = time.monotonic()
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
                logger.info("proxy request running request_id=%s queued_ms=%s", item.request_id, queued_ms)
                await self._wait_for_upstream_interval(item.request_id)
                payload = await item.handler()
            except Exception as exc:
                if item.manage_quota:
                    self.quota_manager.release(item.user_id, item.estimated_cost)
                code, message = self._error_details(exc)
                self.db.execute(
                    """
                    UPDATE usage_logs
                    SET status = 'failed',
                        queued_ms = COALESCE(queued_ms, ?),
                        error_code = ?,
                        error_message = ?,
                        log_level = 'ERROR',
                        completed_at = ?
                    WHERE request_id = ?
                    """,
                    (queued_ms, code, message[:500], utc_now_iso(), item.request_id),
                )
                logger.exception("proxy request failed request_id=%s code=%s", item.request_id, code)
                if not item.future.done():
                    item.future.set_exception(exc)
            else:
                try:
                    if item.process_zip_response:
                        saved_files = archive_zip_images(
                            zip_payload=payload,
                            request_id=item.request_id,
                            action=item.action,
                            config=item.logging_config,
                        )
                    else:
                        saved_files = []
                except Exception:
                    logger.exception("failed to archive generated images request_id=%s", item.request_id)
                    saved_files = []
                if item.manage_quota:
                    self.quota_manager.confirm(item.user_id, item.estimated_cost)
                self.db.execute(
                    """
                    UPDATE usage_logs
                    SET status = 'success',
                        queued_ms = COALESCE(queued_ms, ?),
                        final_anlas_cost = ?,
                        output_files = ?,
                        completed_at = ?
                    WHERE request_id = ?
                    """,
                    (queued_ms, item.estimated_cost, json_dumps(saved_files), utc_now_iso(), item.request_id),
                )
                logger.info(
                    "proxy request succeeded request_id=%s final_cost=%s output_files=%s",
                    item.request_id,
                    item.estimated_cost,
                    len(saved_files),
                )
                if not item.future.done():
                    item.future.set_result(payload)
            finally:
                self._running_item = None
                self._running_started_at = None
                self.queue.task_done()

    @staticmethod
    def _error_details(exc: Exception) -> tuple[str, str]:
        if isinstance(exc, APIError):
            return str(exc.code or "upstream_error"), exc.message
        return exc.__class__.__name__, str(exc)

    async def _wait_for_upstream_interval(self, request_id: str) -> None:
        if self.min_upstream_interval_seconds <= 0 or self._last_upstream_started_at is None:
            self._last_upstream_started_at = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_upstream_started_at
        delay = self.min_upstream_interval_seconds - elapsed
        if delay > 0:
            logger.info("proxy request waiting before upstream request_id=%s delay_seconds=%.3f", request_id, delay)
            await asyncio.sleep(delay)
        self._last_upstream_started_at = time.monotonic()

    @staticmethod
    def _item_snapshot(item: QueueItem, now: float, status: str, position: int) -> dict[str, object]:
        return {
            "request_id": item.request_id,
            "user_id": item.user_id,
            "action": item.action,
            "tier": item.tier,
            "estimated_anlas_cost": item.estimated_cost,
            "priority": item.priority,
            "position": position,
            "status": status,
            "queued_seconds": max(0, int(now - item.enqueued_at)),
        }


class QueueFull(Exception):
    pass
