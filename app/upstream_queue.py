from __future__ import annotations

import asyncio
import dataclasses
import random
import time
from typing import Any, Callable

from novelai_python._exceptions import APIError

from .logging_utils import logger
from .queue_errors import QueueFull, Retry429Error, UpstreamExecutionTimeout, UserUnavailable
from .queue_models import ImageHostingServiceLike, QueueItem
from .queue_snapshot import ProxyQueueSnapshot
from .queue_snapshot_helpers import item_snapshot
from .queue_work_queue import PriorityWorkQueue
from .retry_policy import RetryPolicy
from .usage_logs import UsageLogRepository
from .zip_images import archive_zip_images


class ProxyQueue:
    def __init__(
        self,
        upstream_id: str,
        usage_logs: UsageLogRepository,
        max_queue_size: int,
        client_provider: Callable[[], Any] | None = None,
        upstream_interval_min_seconds: float = 2,
        upstream_interval_max_seconds: float = 5,
        upstream_error_extra_delay_seconds: float = 5,
        upstream_execution_timeout_seconds: float = 60,
        upstream_timeout_cleanup_grace_seconds: float = 60,
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
        # 超时 handler 的清理宽限：超时取消后最多再等这么久让 handler 真正退出，
        # 以维持对上游的串行；0 表示无限等待（旧行为，病态 handler 会卡死整个队列）。
        self.upstream_timeout_cleanup_grace_seconds = max(0.0, float(upstream_timeout_cleanup_grace_seconds))
        self._retry_policy = retry_policy or RetryPolicy(queue_length_threshold=retry_429_queue_length_threshold)
        self.get_total_queue_length = get_total_queue_length
        self._is_user_available = is_user_available or (lambda _user_id: True)
        self._last_upstream_completed_at: float | None = None
        self._apply_error_extra_delay_next = False
        self._worker: asyncio.Task | None = None
        self._stopping = False
        self._image_upload_tasks: set[asyncio.Task] = set()
        self._running_item: QueueItem | None = None
        self._running_started_at: float | None = None
        self._on_change = on_change
        self._on_api_error = on_api_error

    @property
    def on_api_error(self) -> Callable[[str, APIError], None] | None:
        return self._on_api_error

    @on_api_error.setter
    def on_api_error(self, value: Callable[[str, APIError], None] | None) -> None:
        self._on_api_error = value

    @property
    def running_item(self) -> QueueItem | None:
        return self._running_item

    def start(self) -> None:
        self._stopping = False
        if self._worker is None or self._worker.done():
            self._spawn_worker()

    def _spawn_worker(self) -> None:
        self._worker = asyncio.create_task(self._run())
        self._worker.add_done_callback(self._on_worker_done)

    def _on_worker_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if self._stopping:
            return
        # worker 协程是单点：意外退出会让整个上游队列永久停摆（排队项无人消费）。
        # 循环内部已经兜底，这里是最后防线：记录后立即重启。
        logger.error(
            "upstream queue worker exited unexpectedly, restarting upstream_id=%s",
            self.upstream_id,
            exc_info=exc,
        )
        self._spawn_worker()

    async def stop(self, *, drain: bool = True) -> None:
        self._stopping = True
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
            try:
                await self._process_item(item)
            except asyncio.CancelledError:
                # worker 被取消（stop / 移除上游）时正在处理的 attempt 要么已被
                # _process_item 料理，要么在这里取消掉，避免调用方永久挂起。
                if not item.future.done():
                    item.future.cancel()
                raise
            except Exception:
                # _process_item 已经对成功/失败处理各自兜底，这里只可能是
                # 兜底代码本身出错；吞掉并继续，绝不能让 worker 退出。
                logger.exception(
                    "proxy queue item processing crashed request_id=%s upstream_id=%s",
                    item.request_id,
                    self.upstream_id,
                )
            finally:
                self._running_item = None
                self._running_started_at = None
                try:
                    self.queue.task_done()
                except ValueError:
                    logger.exception("proxy queue task_done accounting error upstream_id=%s", self.upstream_id)
                self._notify_change()
                await asyncio.sleep(0)

    async def _process_item(self, item: QueueItem) -> None:
        queued_ms = int((time.monotonic() - item.enqueued_at) * 1000)
        upstream_ms: int | None = None
        try:
            if self._cancel_if_caller_done(item, queued_ms, after_interval=False):
                return
            if self._reject_if_user_unavailable(item):
                return
            self._mark_item_running(item, queued_ms)
            await self._wait_for_upstream_interval(item.request_id)
            if self._cancel_if_caller_done(item, queued_ms, after_interval=True):
                return
            payload, upstream_ms = await self._execute_item(item)
            self._last_upstream_completed_at = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await self._handle_item_exception(item, queued_ms, upstream_ms, exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "proxy request failure handling crashed request_id=%s upstream_id=%s",
                    item.request_id,
                    self.upstream_id,
                )
            finally:
                # 记账/日志写入自身出错时也必须让调用方拿到结果，否则请求悬挂。
                if not item.future.done():
                    item.future.set_exception(exc)
        else:
            try:
                await self._handle_item_success(item, queued_ms, upstream_ms, payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "proxy request success handling crashed request_id=%s upstream_id=%s",
                    item.request_id,
                    self.upstream_id,
                )
                # 上游已经成功产出，记账问题不应把成功变成失败。
                if not item.future.done():
                    item.future.set_result(payload)

    def _cancel_if_caller_done(self, item: QueueItem, queued_ms: int, *, after_interval: bool) -> bool:
        if item.cancel_future is None or not item.cancel_future.done():
            return False
        item.accounting.settle_failure(
            queued_ms=queued_ms,
            error_code="client_cancelled",
            error_message="Client cancelled before upstream execution",
            attempt_number=item.attempt_number,
        )
        if not item.future.done():
            item.future.cancel()
        if after_interval:
            logger.info(
                "proxy request skipped after interval because caller cancelled request_id=%s upstream_id=%s attempt_number=%s",
                item.request_id,
                item.upstream_id,
                item.attempt_number,
            )
        else:
            logger.info(
                "proxy request skipped because caller cancelled request_id=%s upstream_id=%s attempt_number=%s",
                item.request_id,
                item.upstream_id,
                item.attempt_number,
            )
        return True

    def _reject_if_user_unavailable(self, item: QueueItem) -> bool:
        if not item.accounting.manage_quota or self._is_user_available(item.user_id):
            return False
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
        return True

    def _mark_item_running(self, item: QueueItem, queued_ms: int) -> None:
        if not item.is_admin_probe:
            self.usage_logs.mark_running(item.request_id, queued_ms, item.upstream_id, item.attempt_number)
        logger.info(
            "proxy request running request_id=%s upstream_id=%s queued_ms=%s attempt_number=%s",
            item.request_id,
            item.upstream_id,
            queued_ms,
            item.attempt_number,
        )

    async def _execute_item(self, item: QueueItem) -> tuple[bytes, int]:
        upstream_started_at = time.monotonic()
        try:
            payload = await self._execute_handler_with_timeout(item)
        finally:
            upstream_ms = int((time.monotonic() - upstream_started_at) * 1000)
        return payload, upstream_ms

    async def _handle_item_exception(
        self,
        item: QueueItem,
        queued_ms: int,
        upstream_ms: int | None,
        exc: Exception,
    ) -> None:
        self._last_upstream_completed_at = time.monotonic()
        self._notify_upstream_api_error(exc)
        if self._handle_retryable_429(item, queued_ms, upstream_ms, exc):
            return
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

    def _notify_upstream_api_error(self, exc: Exception) -> None:
        if not isinstance(exc, APIError) or self._on_api_error is None:
            return
        try:
            self._on_api_error(self.upstream_id, exc)
        except Exception:
            logger.exception("upstream API error callback failed upstream_id=%s", self.upstream_id)

    def _handle_retryable_429(
        self,
        item: QueueItem,
        queued_ms: int,
        upstream_ms: int | None,
        exc: Exception,
    ) -> bool:
        if item.is_admin_probe or not isinstance(exc, APIError) or str(exc.code) != "429":
            return False
        should_retry = (
            self._retry_policy.should_attempt_429_retry(self.get_total_queue_length())
            if self.get_total_queue_length
            else False
        )
        if not should_retry:
            return False

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
        return True

    async def _handle_item_success(
        self,
        item: QueueItem,
        queued_ms: int,
        upstream_ms: int | None,
        payload: bytes,
    ) -> None:
        saved_files = await self._archive_successful_zip_images(item, payload)
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

    async def _archive_successful_zip_images(self, item: QueueItem, payload: bytes) -> list[dict[str, object]]:
        if not item.process_zip_response or item.is_admin_probe:
            return []
        try:
            return await asyncio.to_thread(
                archive_zip_images,
                zip_payload=payload,
                request_id=item.request_id,
                action=item.action,
                config=item.logging_config,
            )
        except Exception:
            logger.exception("failed to archive generated images request_id=%s", item.request_id)
            return []

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
        grace = self.upstream_timeout_cleanup_grace_seconds
        try:
            done, _pending = await asyncio.wait(
                {handler_task},
                timeout=grace if grace > 0 else None,
            )
        except asyncio.CancelledError:
            if not handler_task.done():
                handler_task.add_done_callback(self._consume_timed_out_handler_result)
                logger.info("timed out upstream handler cleanup interrupted request_id=%s", request_id)
            raise
        if handler_task in done:
            if not handler_task.cancelled() and handler_task.exception() is not None:
                logger.debug("timed out upstream handler finished with an exception request_id=%s", request_id)
            return
        # handler 在取消后的宽限期内仍未退出（吞掉了取消或卡在不可中断的调用上）。
        # 继续等待会让 worker 永久阻塞、整个上游队列卡死，因此放弃等待并继续消费。
        handler_task.add_done_callback(self._consume_timed_out_handler_result)
        logger.warning(
            "timed out upstream handler did not exit within cleanup grace, resuming queue request_id=%s upstream_id=%s grace_seconds=%s",
            request_id,
            self.upstream_id,
            grace,
        )

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
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:
            # 状态广播只是 UI 优化，绝不能反噬 worker 循环。
            logger.exception("queue change callback failed upstream_id=%s", self.upstream_id)
