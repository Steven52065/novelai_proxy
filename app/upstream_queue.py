from __future__ import annotations

import asyncio
import dataclasses
import random
import time
from typing import Any, Callable

from novelai_python._exceptions import APIError

from .logging_utils import archive_zip_images, logger
from .quota_manager import QuotaManager
from .queue_errors import QueueFull, Retry429Error, UpstreamExecutionTimeout, UserUnavailable
from .queue_models import ImageHostingServiceLike, QueueItem
from .queue_snapshot import ProxyQueueSnapshot
from .queue_snapshot_helpers import item_snapshot
from .queue_work_queue import PriorityWorkQueue
from .retry_policy import RetryPolicy
from .usage_logs import UsageLogRepository


class ProxyQueue:
    def __init__(
        self,
        upstream_id: str,
        quota_manager: QuotaManager,
        usage_logs: UsageLogRepository,
        max_queue_size: int,
        client_provider: Callable[[], Any] | None = None,
        upstream_interval_min_seconds: float = 2,
        upstream_interval_max_seconds: float = 5,
        upstream_error_extra_delay_seconds: float = 5,
        upstream_execution_timeout_seconds: float = 60,
        retry_429_queue_length_threshold: int = 3,
        retry_policy: RetryPolicy | None = None,
        get_total_queue_length: Callable[[], int] | None = None,
        is_user_available: Callable[[int], bool] | None = None,
        image_hosting: ImageHostingServiceLike | None = None,
        on_change: Callable[[], None] | None = None,
        on_api_error: Callable[[str, APIError], None] | None = None,
    ):
        self.upstream_id = upstream_id
        self.usage_logs = usage_logs
        self.client_provider = client_provider
        self.image_hosting = image_hosting
        self.queue = PriorityWorkQueue(maxsize=max_queue_size)
        self.upstream_interval_min_seconds = max(0.0, float(upstream_interval_min_seconds))
        self.upstream_interval_max_seconds = max(0.0, float(upstream_interval_max_seconds))
        if self.upstream_interval_max_seconds < self.upstream_interval_min_seconds:
            raise ValueError("upstream_interval_max_seconds must be greater than or equal to upstream_interval_min_seconds")
        self.upstream_error_extra_delay_seconds = max(0.0, float(upstream_error_extra_delay_seconds))
        self.upstream_execution_timeout_seconds = float(upstream_execution_timeout_seconds)
        if self.upstream_execution_timeout_seconds <= 0:
            raise ValueError("upstream_execution_timeout_seconds must be greater than 0")
        self._retry_policy = retry_policy or RetryPolicy(queue_length_threshold=retry_429_queue_length_threshold)
        self.get_total_queue_length = get_total_queue_length
        self._is_user_available = is_user_available or (lambda _user_id: True)
        self._last_upstream_completed_at: float | None = None
        self._apply_error_extra_delay_next = False
        self._worker: asyncio.Task | None = None
        self._image_upload_tasks: set[asyncio.Task] = set()
        self._running_item: QueueItem | None = None
        self._running_started_at: float | None = None
        self._on_change = on_change
        self._on_api_error = on_api_error

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def stop(self, *, drain: bool = True) -> None:
        if self._worker is None:
            return
        if drain:
            await self.queue.join()
            await self.wait_for_image_uploads()
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

    def snapshot(self) -> ProxyQueueSnapshot:
        now = time.monotonic()
        queued = [
            item_snapshot(item, now, "queued", position=index)
            for index, item in enumerate(self.queue.snapshot_items(), start=1)
        ]
        running = None
        if self._running_item is not None:
            running = item_snapshot(self._running_item, now, "running", position=0)
            if self._running_started_at is not None:
                running["running_seconds"] = max(0, int(now - self._running_started_at))
        return {
            "queue_size": len(queued),
            "running": running,
            "queued": queued,
        }

    def enqueue(self, item: QueueItem, *, allow_overflow: bool = False) -> asyncio.Future:
        if self.queue.full() and not allow_overflow:
            raise QueueFull

        loop = asyncio.get_running_loop()
        attempt_future: asyncio.Future = loop.create_future()
        retry_attempt_logged = item.retry_attempt_logged
        if item.attempt_number > 0 and not retry_attempt_logged:
            item.accounting.record_retry_attempt(
                attempt_number=item.attempt_number,
                upstream_id=self.upstream_id,
            )
            retry_attempt_logged = True
        attempt_item = dataclasses.replace(
            item,
            enqueued_at=time.monotonic(),
            upstream_id=self.upstream_id,
            future=attempt_future,
            cancel_future=item.future,
            retry_attempt_logged=retry_attempt_logged,
        )

        try:
            self.queue.put_nowait(attempt_item, allow_overflow=allow_overflow)
        except asyncio.QueueFull as exc:
            raise QueueFull from exc
        self._notify_change()
        return attempt_future

    def extract_pending_items(self) -> list[QueueItem]:
        items = self.queue.remove_matching(lambda _item: True)
        if items:
            self._notify_change()
        return items

    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            self._running_item = item
            self._running_started_at = time.monotonic()
            self._notify_change()
            queued_ms = int((time.monotonic() - item.enqueued_at) * 1000)
            upstream_ms: int | None = None
            try:
                if item.cancel_future is not None and item.cancel_future.done():
                    item.accounting.settle_failure(
                        queued_ms=queued_ms,
                        error_code="client_cancelled",
                        error_message="Client cancelled before upstream execution",
                        attempt_number=item.attempt_number,
                    )
                    if not item.future.done():
                        item.future.cancel()
                    logger.info(
                        "proxy request skipped because caller cancelled request_id=%s upstream_id=%s attempt_number=%s",
                        item.request_id,
                        item.upstream_id,
                        item.attempt_number,
                    )
                    continue
                if item.accounting.manage_quota and not self._is_user_available(item.user_id):
                    item.accounting.settle_rejected(
                        error_code="user_unavailable",
                        error_message="User is no longer active",
                        log_level="INFO",
                        attempt_number=item.attempt_number,
                    )
                    if not item.future.done():
                        item.future.set_exception(UserUnavailable("User is no longer active"))
                    logger.info(
                        "proxy request skipped because user is unavailable request_id=%s user_id=%s attempt_number=%s",
                        item.request_id,
                        item.user_id,
                        item.attempt_number,
                    )
                    continue
                if not item.is_admin_probe:
                    self.usage_logs.mark_running(item.request_id, queued_ms, item.upstream_id, item.attempt_number)
                logger.info(
                    "proxy request running request_id=%s upstream_id=%s queued_ms=%s attempt_number=%s",
                    item.request_id,
                    item.upstream_id,
                    queued_ms,
                    item.attempt_number,
                )
                await self._wait_for_upstream_interval(item.request_id)
                if item.cancel_future is not None and item.cancel_future.done():
                    item.accounting.settle_failure(
                        queued_ms=queued_ms,
                        error_code="client_cancelled",
                        error_message="Client cancelled before upstream execution",
                        attempt_number=item.attempt_number,
                    )
                    if not item.future.done():
                        item.future.cancel()
                    logger.info(
                        "proxy request skipped after interval because caller cancelled request_id=%s upstream_id=%s attempt_number=%s",
                        item.request_id,
                        item.upstream_id,
                        item.attempt_number,
                    )
                    continue
                upstream_started_at = time.monotonic()
                try:
                    payload = await self._execute_handler_with_timeout(item)
                finally:
                    upstream_ms = int((time.monotonic() - upstream_started_at) * 1000)
                self._last_upstream_completed_at = time.monotonic()
            except Exception as exc:
                self._last_upstream_completed_at = time.monotonic()
                if isinstance(exc, APIError) and self._on_api_error is not None:
                    try:
                        self._on_api_error(self.upstream_id, exc)
                    except Exception:
                        logger.exception("upstream API error callback failed upstream_id=%s", self.upstream_id)

                if not item.is_admin_probe and isinstance(exc, APIError) and str(exc.code) == "429":
                    should_retry = (
                        self._retry_policy.should_attempt_429_retry(self.get_total_queue_length())
                        if self.get_total_queue_length
                        else False
                    )
                    if should_retry:
                        code, message = self._error_details(exc)
                        item.accounting.record_retry_failure(
                            queued_ms=queued_ms,
                            error_code=code,
                            error_message=message,
                            upstream_ms=upstream_ms,
                            attempt_number=item.attempt_number,
                        )
                        total_queue_length = self.get_total_queue_length() if self.get_total_queue_length else self.queue.qsize()
                        logger.warning(
                            "proxy request 429 error, will retry request_id=%s attempt_number=%s total_queue_length=%s threshold=%s",
                            item.request_id,
                            item.attempt_number,
                            total_queue_length,
                            self._retry_policy.queue_length_threshold,
                        )
                        self._apply_error_extra_delay_next = True
                        if not item.future.done():
                            item.future.set_exception(Retry429Error(exc))
                        continue

                if not item.is_admin_probe and isinstance(exc, (APIError, UpstreamExecutionTimeout)):
                    self._apply_error_extra_delay_next = True

                code, message = self._error_details(exc)
                item.accounting.settle_failure(
                    queued_ms=queued_ms,
                    error_code=code,
                    error_message=message,
                    upstream_ms=upstream_ms,
                    attempt_number=item.attempt_number,
                )
                logger.exception("proxy request failed request_id=%s code=%s", item.request_id, code)
                if not item.future.done():
                    item.future.set_exception(exc)
                if isinstance(exc, UpstreamExecutionTimeout):
                    await self._wait_for_timed_out_handler(item.request_id, exc.handler_task)
                    self._last_upstream_completed_at = time.monotonic()
            else:
                saved_files = []
                if item.process_zip_response and not item.is_admin_probe:
                    try:
                        saved_files = await asyncio.to_thread(
                            archive_zip_images,
                            zip_payload=payload,
                            request_id=item.request_id,
                            action=item.action,
                            config=item.logging_config,
                        )
                    except Exception:
                        logger.exception("failed to archive generated images request_id=%s", item.request_id)
                        saved_files = []
                item.accounting.settle_success(
                    queued_ms=queued_ms,
                    final_cost=item.estimated_cost,
                    output_files=saved_files,
                    upstream_ms=upstream_ms,
                    is_retry_success=item.has_retried_429,
                    attempt_number=item.attempt_number,
                )
                log_color = "white" if item.has_retried_429 else "default"
                logger.info(
                    "proxy request succeeded request_id=%s final_cost=%s output_files=%s is_retry_success=%s log_color=%s",
                    item.request_id,
                    item.estimated_cost,
                    len(saved_files),
                    item.has_retried_429,
                    log_color,
                )
                if not item.future.done():
                    item.future.set_result(payload)
                if item.process_zip_response and not item.is_admin_probe and self.image_hosting is not None:
                    self._schedule_image_upload(zip_payload=payload, request_id=item.request_id, attempt_number=item.attempt_number)
            finally:
                self._running_item = None
                self._running_started_at = None
                self.queue.task_done()
                self._notify_change()
                await asyncio.sleep(0)

    @staticmethod
    def _error_details(exc: Exception) -> tuple[str, str]:
        if isinstance(exc, UpstreamExecutionTimeout):
            return "upstream_timeout", str(exc)
        if isinstance(exc, APIError):
            return str(exc.code or "upstream_error"), exc.message
        return exc.__class__.__name__, str(exc)

    async def _execute_handler_with_timeout(self, item: QueueItem) -> bytes:
        client = self.client_provider() if self.client_provider is not None else None
        handler_task = asyncio.create_task(item.handler(client))
        try:
            done, _pending = await asyncio.wait(
                {handler_task},
                timeout=self.upstream_execution_timeout_seconds,
            )
        except asyncio.CancelledError:
            handler_task.cancel()
            handler_task.add_done_callback(self._consume_timed_out_handler_result)
            raise
        if handler_task in done:
            return handler_task.result()
        handler_task.cancel()
        raise UpstreamExecutionTimeout(self.upstream_execution_timeout_seconds, handler_task=handler_task)

    async def _wait_for_timed_out_handler(self, request_id: str, handler_task: asyncio.Task | None) -> None:
        if handler_task is None:
            return
        try:
            await asyncio.shield(handler_task)
        except asyncio.CancelledError:
            if not handler_task.done():
                handler_task.add_done_callback(self._consume_timed_out_handler_result)
                logger.info("timed out upstream handler cleanup interrupted request_id=%s", request_id)
                raise
        except Exception:
            logger.debug("timed out upstream handler finished with an exception request_id=%s", request_id, exc_info=True)

    @staticmethod
    def _consume_timed_out_handler_result(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    async def _wait_for_upstream_interval(self, request_id: str) -> None:
        interval = self._next_upstream_interval()
        extra_delay = self.upstream_error_extra_delay_seconds if self._apply_error_extra_delay_next else 0.0
        self._apply_error_extra_delay_next = False
        required_delay = interval + extra_delay
        if required_delay <= 0:
            return
        if self._last_upstream_completed_at is None:
            return
        elapsed = time.monotonic() - self._last_upstream_completed_at
        delay = max(required_delay - elapsed, 0.0)
        if delay > 0:
            logger.info(
                "proxy request waiting after last completion request_id=%s delay_seconds=%.3f interval_seconds=%.3f error_extra_delay_seconds=%.3f",
                request_id,
                delay,
                interval,
                extra_delay,
            )
            await asyncio.sleep(delay)

    def _next_upstream_interval(self) -> float:
        if self.upstream_interval_max_seconds == self.upstream_interval_min_seconds:
            return self.upstream_interval_min_seconds
        return random.uniform(self.upstream_interval_min_seconds, self.upstream_interval_max_seconds)

    def _schedule_image_upload(self, *, zip_payload: bytes, request_id: str, attempt_number: int) -> None:
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
        task = asyncio.create_task(self._upload_images_and_update_log(zip_payload=zip_payload, request_id=request_id, attempt_number=attempt_number))
        self._image_upload_tasks.add(task)
        task.add_done_callback(self._image_upload_tasks.discard)

    async def _upload_images_and_update_log(self, *, zip_payload: bytes, request_id: str, attempt_number: int) -> None:
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
        self.usage_logs.update_image_urls(request_id, image_urls, attempt_number)
        logger.info("image host upload succeeded request_id=%s image_urls=%s attempt_number=%s", request_id, len(image_urls), attempt_number)

    def _notify_change(self) -> None:
        if self._on_change is not None:
            self._on_change()
