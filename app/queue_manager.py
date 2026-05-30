from __future__ import annotations

import asyncio
import itertools
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from novelai_python._exceptions import APIError

from .config import LoggingConfig
from .database import Database, utc_now_iso
from .logging_utils import archive_zip_images, json_dumps, logger
from .quota_manager import QuotaManager


class ImageHostingServiceLike(Protocol):
    max_pending_uploads: int

    async def upload_zip_images(self, *, zip_payload: bytes, request_id: str) -> list[dict[str, object]]:
        ...


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
        upstream_interval_min_seconds: float = 2,
        upstream_interval_max_seconds: float = 5,
        upstream_error_extra_delay_seconds: float = 5,
        image_hosting: ImageHostingServiceLike | None = None,
    ):
        self.db = db
        self.quota_manager = quota_manager
        self.image_hosting = image_hosting
        self.queue: asyncio.PriorityQueue[QueueItem] = asyncio.PriorityQueue(maxsize=max_queue_size)
        self.upstream_interval_min_seconds = max(0.0, float(upstream_interval_min_seconds))
        self.upstream_interval_max_seconds = max(0.0, float(upstream_interval_max_seconds))
        if self.upstream_interval_max_seconds < self.upstream_interval_min_seconds:
            raise ValueError("upstream_interval_max_seconds must be greater than or equal to upstream_interval_min_seconds")
        self.upstream_error_extra_delay_seconds = max(0.0, float(upstream_error_extra_delay_seconds))
        self._last_upstream_started_at: float | None = None
        self._last_upstream_interval_seconds = 0.0
        self._apply_error_extra_delay_next = False
        self._sequence = itertools.count()
        self._worker: asyncio.Task | None = None
        self._image_upload_tasks: set[asyncio.Task] = set()
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
        if self._image_upload_tasks:
            for task in self._image_upload_tasks:
                task.cancel()
            await asyncio.gather(*self._image_upload_tasks, return_exceptions=True)
            self._image_upload_tasks.clear()

    async def wait_for_image_uploads(self) -> None:
        if self._image_upload_tasks:
            await asyncio.gather(*self._image_upload_tasks)

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
                if isinstance(exc, APIError):
                    self._apply_error_extra_delay_next = True
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
                saved_files = []
                if item.process_zip_response:
                    try:
                        saved_files = archive_zip_images(
                            zip_payload=payload,
                            request_id=item.request_id,
                            action=item.action,
                            config=item.logging_config,
                        )
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
                        image_urls = COALESCE(image_urls, ?),
                        completed_at = ?
                    WHERE request_id = ?
                    """,
                    (
                        queued_ms,
                        item.estimated_cost,
                        json_dumps(saved_files),
                        json_dumps([]),
                        utc_now_iso(),
                        item.request_id,
                    ),
                )
                logger.info(
                    "proxy request succeeded request_id=%s final_cost=%s output_files=%s",
                    item.request_id,
                    item.estimated_cost,
                    len(saved_files),
                )
                if not item.future.done():
                    item.future.set_result(payload)
                if item.process_zip_response and self.image_hosting is not None:
                    self._schedule_image_upload(zip_payload=payload, request_id=item.request_id)
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
        interval = self._next_upstream_interval()
        extra_delay = self.upstream_error_extra_delay_seconds if self._apply_error_extra_delay_next else 0.0
        self._apply_error_extra_delay_next = False
        required_interval = interval + extra_delay
        if required_interval <= 0 or self._last_upstream_started_at is None:
            self._last_upstream_started_at = time.monotonic()
            self._last_upstream_interval_seconds = interval
            return
        elapsed = time.monotonic() - self._last_upstream_started_at
        delay = required_interval - elapsed
        if delay > 0:
            logger.info(
                "proxy request waiting before upstream request_id=%s delay_seconds=%.3f interval_seconds=%.3f error_extra_delay_seconds=%.3f",
                request_id,
                delay,
                interval,
                extra_delay,
            )
            await asyncio.sleep(delay)
        self._last_upstream_started_at = time.monotonic()
        self._last_upstream_interval_seconds = interval

    def _next_upstream_interval(self) -> float:
        if self.upstream_interval_max_seconds == self.upstream_interval_min_seconds:
            return self.upstream_interval_min_seconds
        return random.uniform(self.upstream_interval_min_seconds, self.upstream_interval_max_seconds)

    def _schedule_image_upload(self, *, zip_payload: bytes, request_id: str) -> None:
        if self.image_hosting is None:
            return
        max_pending = int(self.image_hosting.max_pending_uploads)
        pending = len(self._image_upload_tasks)
        if max_pending > 0 and pending >= max_pending:
            logger.warning(
                "image host upload skipped request_id=%s reason=pending_limit pending=%s max_pending=%s",
                request_id,
                pending,
                max_pending,
            )
            return
        task = asyncio.create_task(self._upload_images_and_update_log(zip_payload=zip_payload, request_id=request_id))
        self._image_upload_tasks.add(task)
        task.add_done_callback(self._image_upload_tasks.discard)

    async def _upload_images_and_update_log(self, *, zip_payload: bytes, request_id: str) -> None:
        if self.image_hosting is None:
            return
        try:
            image_urls = await self.image_hosting.upload_zip_images(
                zip_payload=zip_payload,
                request_id=request_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("failed to upload generated images request_id=%s", request_id)
            return
        if not image_urls:
            return
        self.db.execute(
            """
            UPDATE usage_logs
            SET image_urls = ?
            WHERE request_id = ?
            """,
            (json_dumps(image_urls), request_id),
        )
        logger.info("image host upload succeeded request_id=%s image_urls=%s", request_id, len(image_urls))

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
