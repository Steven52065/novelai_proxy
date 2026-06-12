from __future__ import annotations

import asyncio
import dataclasses
import itertools
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

from novelai_python._exceptions import APIError

from .config import LoggingConfig
from .logging_utils import archive_zip_images, logger
from .quota_manager import QuotaManager
from .request_accounting import RequestAccounting
from .usage_logs import UsageLogRepository


class ImageHostingServiceLike(Protocol):
    max_pending_uploads: int

    async def upload_zip_images(self, *, zip_payload: bytes, request_id: str) -> list[dict[str, object]]:
        ...


@dataclass(order=True)
class QueueItem:
    """单个代理请求在两层队列中流转的上下文。

    同一个 dataclass 同时服务于两层队列：

    - 调度层（``RoutingProxyQueue`` 的 dispatch 队列）：``upstream_id`` 为
      ``None`` 表示尚未路由，``future`` 是面向调用方的最终 future，
      ``cancel_future`` 为 ``None``。
    - 执行层（每上游 ``ProxyQueue``）：``upstream_id`` 指向已选定的上游，
      ``future`` 是本次 attempt 的 future（由调度层注册 done_callback），
      ``cancel_future`` 指向调用方 future（用于检测客户端取消）。

    两层之间通过 ``dataclasses.replace`` 整体复制，新增业务字段会自动
    随之流动，无需在多个签名间手工同步。
    """

    priority: int
    sequence: int
    enqueued_at: float = field(compare=False)
    request_id: str = field(compare=False)
    user_id: int = field(compare=False)
    action: str = field(compare=False)
    tier: str = field(compare=False)
    estimated_cost: int = field(compare=False)
    accounting: RequestAccounting = field(compare=False)
    logging_config: LoggingConfig = field(compare=False)
    process_zip_response: bool = field(compare=False)
    handler: Callable[[Any], Awaitable[bytes]] = field(compare=False)
    future: asyncio.Future = field(compare=False)
    upstream_id: str | None = field(default=None, compare=False)
    allowed_upstreams: frozenset[str] | set[str] | list[str] | None = field(default=None, compare=False)
    cancel_future: asyncio.Future | None = field(default=None, compare=False)
    has_retried_429: bool = field(default=False, compare=False)
    attempt_number: int = field(default=0, compare=False)
    last_429_error: APIError | None = field(default=None, compare=False)


@dataclass(frozen=True)
class UpstreamQueueTarget:
    id: str
    client_provider: Callable[[], Any]


@dataclass
class AdaptiveUpstreamScore:
    score: float


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
        get_total_queue_length: callable | None = None,
        image_hosting: ImageHostingServiceLike | None = None,
        on_change: Callable[[], None] | None = None,
    ):
        self.upstream_id = upstream_id
        self.quota_manager = quota_manager
        self.usage_logs = usage_logs
        self.client_provider = client_provider
        self.image_hosting = image_hosting
        self.queue: asyncio.PriorityQueue[QueueItem] = asyncio.PriorityQueue(maxsize=max_queue_size)
        self.upstream_interval_min_seconds = max(0.0, float(upstream_interval_min_seconds))
        self.upstream_interval_max_seconds = max(0.0, float(upstream_interval_max_seconds))
        if self.upstream_interval_max_seconds < self.upstream_interval_min_seconds:
            raise ValueError("upstream_interval_max_seconds must be greater than or equal to upstream_interval_min_seconds")
        self.upstream_error_extra_delay_seconds = max(0.0, float(upstream_error_extra_delay_seconds))
        self.upstream_execution_timeout_seconds = float(upstream_execution_timeout_seconds)
        if self.upstream_execution_timeout_seconds <= 0:
            raise ValueError("upstream_execution_timeout_seconds must be greater than 0")
        self.retry_429_queue_length_threshold = int(retry_429_queue_length_threshold)
        self.get_total_queue_length = get_total_queue_length
        self._last_upstream_completed_at: float | None = None
        self._apply_error_extra_delay_next = False
        self._worker: asyncio.Task | None = None
        self._image_upload_tasks: set[asyncio.Task] = set()
        self._running_item: QueueItem | None = None
        self._running_started_at: float | None = None
        self._on_change = on_change

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

    def snapshot(self) -> dict[str, object]:
        now = time.monotonic()
        queued = [
            _item_snapshot(item, now, "queued", position=index)
            for index, item in enumerate(sorted(self.queue._queue), start=1)
        ]
        running = None
        if self._running_item is not None:
            running = _item_snapshot(self._running_item, now, "running", position=0)
            if self._running_started_at is not None:
                running["running_seconds"] = max(0, int(now - self._running_started_at))
        return {
            "queue_size": len(queued),
            "running": running,
            "queued": queued,
        }

    def enqueue(self, item: QueueItem, *, allow_overflow: bool = False) -> asyncio.Future:
        """把调度层的 item 绑定到本上游后入队，返回本次 attempt 的 future。

        传入的 item 不会被修改：本方法用 ``dataclasses.replace`` 复制出
        执行层 item（``upstream_id`` 指向本上游、``future`` 换成新建的
        attempt future、``cancel_future`` 指向调用方 future、排队计时重新
        开始），调用方持有的调度层 item 仍指向最终 future。
        """
        if self.queue.full() and not allow_overflow:
            raise QueueFull

        loop = asyncio.get_running_loop()
        attempt_future: asyncio.Future = loop.create_future()
        attempt_item = dataclasses.replace(
            item,
            enqueued_at=time.monotonic(),
            upstream_id=self.upstream_id,
            future=attempt_future,
            cancel_future=item.future,
        )
        # 如果是重试（attempt_number > 0），先插入新的数据库记录。
        # 这里没有 await，插入完成前 worker 不会开始处理刚入队的 item。
        # 插入失败时由 accounting 负责释放预留额度后重新抛出。
        if attempt_item.attempt_number > 0:
            attempt_item.accounting.record_retry_attempt(
                attempt_number=attempt_item.attempt_number,
                upstream_id=self.upstream_id,
            )

        try:
            if self.queue.full() and allow_overflow:
                self.queue._put(attempt_item)
                self.queue._unfinished_tasks += 1
                self.queue._finished.clear()
                self.queue._wakeup_next(self.queue._getters)
            else:
                self.queue.put_nowait(attempt_item)
        except asyncio.QueueFull as exc:
            raise QueueFull from exc
        self._notify_change()
        return attempt_future

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
                if item.accounting.manage_quota and not _user_is_available(self.quota_manager, item.user_id):
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
                # 记录请求完成时间（成功情况）
                self._last_upstream_completed_at = time.monotonic()
            except Exception as exc:
                # 记录请求完成时间（失败情况）
                self._last_upstream_completed_at = time.monotonic()

                # 检查是否为 429 错误且应该重试
                if isinstance(exc, APIError) and str(exc.code) == "429":
                    should_retry = self._should_retry_429(self.get_total_queue_length) if self.get_total_queue_length else False
                    if should_retry:
                        # 将 429 错误标记到日志，但状态仍为 failed
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
                            self.retry_429_queue_length_threshold,
                        )
                        # 429 是 API 错误，需要对下一个请求应用额外延迟
                        self._apply_error_extra_delay_next = True
                        # 抛出 Retry429Error 让调度层（RoutingProxyQueue）按常规路由策略重新调度。
                        if not item.future.done():
                            item.future.set_exception(Retry429Error(exc))
                        # 重要：429 重试时不释放额度，因为请求还在重试中，额度应保持 reserved 状态。
                        # 如果最终超过最大尝试次数，调度层会统一释放额度。
                        # 注意：不在这里调用 task_done()，由 finally 块统一处理。
                        continue

                # 所有 API 错误（包括不满足重试条件的 429）都应用额外延迟
                if isinstance(exc, (APIError, UpstreamExecutionTimeout)):
                    self._apply_error_extra_delay_next = True

                # 普通错误处理：释放额度、记录日志、设置异常
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
                    # 调用方可以立刻收到超时，但同一上游槽位要等 handler 真正结束后才能释放。
                    await self._wait_for_timed_out_handler(item.request_id, exc.handler_task)
                    self._last_upstream_completed_at = time.monotonic()
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
                if item.process_zip_response and self.image_hosting is not None:
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

    def _should_retry_429(self, get_total_queue_length: callable) -> bool:
        """检查当前队列状态是否允许重试 429 错误

        Args:
            get_total_queue_length: 获取所有上游总排队长度的回调函数
        """
        if self.retry_429_queue_length_threshold < 0:
            return False
        total_queue_length = get_total_queue_length()
        return total_queue_length <= self.retry_429_queue_length_threshold

    async def _wait_for_upstream_interval(self, request_id: str) -> None:
        interval = self._next_upstream_interval()
        extra_delay = self.upstream_error_extra_delay_seconds if self._apply_error_extra_delay_next else 0.0
        self._apply_error_extra_delay_next = False
        required_delay = interval + extra_delay
        if required_delay <= 0:
            return
        if self._last_upstream_completed_at is None:
            # 首次请求，无需等待
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


def _item_snapshot(item: QueueItem, now: float, status: str, position: int) -> dict[str, object]:
    return {
        "request_id": item.request_id,
        "user_id": item.user_id,
        "action": item.action,
        "tier": item.tier,
        "upstream_id": item.upstream_id,
        "estimated_anlas_cost": item.estimated_cost,
        "priority": item.priority,
        "sequence": item.sequence,
        "position": position,
        "status": status,
        "queued_seconds": max(0, int(now - item.enqueued_at)),
    }


class QueueFull(Exception):
    pass


class QueueClosed(Exception):
    pass


class NoAvailableUpstream(Exception):
    pass


class UserUnavailable(Exception):
    pass


class UpstreamExecutionTimeout(Exception):
    def __init__(self, timeout_seconds: float, *, handler_task: asyncio.Task | None = None):
        self.timeout_seconds = timeout_seconds
        self.handler_task = handler_task
        super().__init__(f"Upstream execution exceeded {timeout_seconds:g} seconds")


class Retry429Error(Exception):
    """Raised when a 429 error should be retried at the routing layer."""
    def __init__(self, original_error: APIError):
        self.original_error = original_error
        super().__init__(str(original_error))


def _without_sequence(item: dict[str, object]) -> dict[str, object]:
    clean = dict(item)
    clean.pop("sequence", None)
    return clean


def _user_is_available(quota_manager: QuotaManager, user_id: int) -> bool:
    db = getattr(quota_manager, "db", None)
    if db is None:
        return True
    row = db.query_one(
        "SELECT is_active, deleted_at FROM users WHERE id = ?",
        (user_id,),
    )
    return row is not None and bool(row["is_active"]) and row["deleted_at"] is None


class RoutingProxyQueue:
    VIP_PRIORITY = 0
    NORMAL_PRIORITY = 10
    VIP_RETRY_PRIORITY = -1

    def __init__(
        self,
        *,
        targets: list[UpstreamQueueTarget],
        quota_manager: QuotaManager,
        usage_logs: UsageLogRepository,
        max_queue_size: int,
        dispatch_max_queue_size: int | None = None,
        routing_strategy: Literal["round_robin", "random", "adaptive_weighted_random"] = "round_robin",
        adaptive_initial_score: float = 0.8,
        adaptive_alpha: float = 0.4,
        adaptive_min_weight: float = 0.15,
        upstream_interval_min_seconds: float = 2,
        upstream_interval_max_seconds: float = 5,
        upstream_error_extra_delay_seconds: float = 5,
        upstream_execution_timeout_seconds: float = 60,
        retry_429_queue_length_threshold: int = 3,
        retry_429_max_attempts: int = 5,
        image_hosting: ImageHostingServiceLike | None = None,
        on_change: Callable[[], None] | None = None,
    ):
        self._quota_manager = quota_manager
        self._usage_logs = usage_logs
        self._retry_429_max_attempts = int(retry_429_max_attempts)
        enabled_targets = [target for target in targets if target.id]
        if not enabled_targets:
            raise ValueError("at least one upstream target is required")
        self.routing_strategy = routing_strategy
        self._image_hosting = image_hosting
        self._targets = {target.id: target for target in enabled_targets}
        self._target_order = [target.id for target in enabled_targets]
        self._round_robin = itertools.count()
        self._sequence = itertools.count()
        self._adaptive_alpha = max(0.0, min(1.0, float(adaptive_alpha)))
        self._adaptive_min_weight = max(0.0, float(adaptive_min_weight))
        initial_score = max(0.0, min(1.0, float(adaptive_initial_score)))
        self._adaptive_scores = {
            target.id: AdaptiveUpstreamScore(score=initial_score)
            for target in enabled_targets
        }
        if dispatch_max_queue_size is None:
            dispatch_max_queue_size = max_queue_size * len(enabled_targets)
        self._dispatch_queue: asyncio.PriorityQueue[QueueItem] = asyncio.PriorityQueue(maxsize=dispatch_max_queue_size)
        self._dispatch_worker: asyncio.Task | None = None
        self._dispatch_running_item: QueueItem | None = None
        self._accepting = True
        self._active_futures: set[asyncio.Future] = set()
        self._on_change = on_change
        self._queues = {
            target.id: ProxyQueue(
                upstream_id=target.id,
                quota_manager=quota_manager,
                usage_logs=usage_logs,
                max_queue_size=max_queue_size,
                client_provider=target.client_provider,
                upstream_interval_min_seconds=upstream_interval_min_seconds,
                upstream_interval_max_seconds=upstream_interval_max_seconds,
                upstream_error_extra_delay_seconds=upstream_error_extra_delay_seconds,
                upstream_execution_timeout_seconds=upstream_execution_timeout_seconds,
                retry_429_queue_length_threshold=retry_429_queue_length_threshold,
                get_total_queue_length=self._get_total_queue_length,
                image_hosting=image_hosting,
                on_change=on_change,
            )
            for target in enabled_targets
        }

    @property
    def image_hosting(self) -> ImageHostingServiceLike | None:
        return self._image_hosting

    @image_hosting.setter
    def image_hosting(self, value: ImageHostingServiceLike | None) -> None:
        self._image_hosting = value
        for queue in self._queues.values():
            queue.image_hosting = value

    def start(self) -> None:
        if self._dispatch_worker is None or self._dispatch_worker.done():
            self._dispatch_worker = asyncio.create_task(self._run_dispatcher())
        for queue in self._queues.values():
            queue.start()

    async def stop(self, *, drain: bool = True) -> None:
        self._accepting = False
        if drain:
            await self._wait_for_active_futures()
        if self._dispatch_worker is not None:
            self._dispatch_worker.cancel()
            try:
                await self._dispatch_worker
            except asyncio.CancelledError:
                pass
        await asyncio.gather(*(queue.stop(drain=drain) for queue in self._queues.values()))

    async def wait_for_image_uploads(self) -> None:
        await asyncio.gather(*(queue.wait_for_image_uploads() for queue in self._queues.values()))

    def qsize(self) -> int:
        return self._dispatch_queue.qsize() + sum(queue.qsize() for queue in self._queues.values())

    def snapshot(self) -> dict[str, object]:
        upstream_snapshots = []
        flattened_running = []
        flattened_queued = []
        now = time.monotonic()
        dispatch_queued = [
            _item_snapshot(item, now, "dispatch_queued", position=index)
            for index, item in enumerate(sorted(self._dispatch_queue._queue), start=1)
        ]
        for upstream_id in self._target_order:
            upstream_snapshot = self._queues[upstream_id].snapshot()
            upstream_snapshot = {"id": upstream_id, **upstream_snapshot}
            if upstream_snapshot["running"] is not None:
                upstream_snapshot["running"].pop("sequence", None)
                flattened_running.append(upstream_snapshot["running"])
            flattened_queued.extend(upstream_snapshot["queued"])
            upstream_snapshot["queued"] = [_without_sequence(item) for item in upstream_snapshot["queued"]]
            upstream_snapshots.append(upstream_snapshot)

        flattened_queued = sorted(dispatch_queued + flattened_queued, key=lambda item: (item["priority"], item["sequence"]))
        for index, item in enumerate(flattened_queued, start=1):
            item["position"] = index
            item.pop("sequence", None)

        return {
            "queue_size": len(flattened_queued),
            "running": flattened_running[0] if len(flattened_running) == 1 else None,
            "running_items": flattened_running,
            "queued": flattened_queued,
            "dispatch_queue_size": len(dispatch_queued),
            "upstreams": upstream_snapshots,
        }

    def get_weights(self) -> dict[str, object]:
        upstreams = []
        for upstream_id in self._target_order:
            score = self._adaptive_scores.get(upstream_id)
            weight = self._adaptive_weight(upstream_id)
            upstreams.append({
                "id": upstream_id,
                "score": round(score.score, 4) if score else 0.0,
                "weight": round(weight, 4),
                "queue_size": self._queues[upstream_id].qsize(),
                "running": self._queues[upstream_id]._running_item is not None,
            })
        return {
            "strategy": self.routing_strategy,
            "upstreams": upstreams,
        }

    def _get_total_queue_length(self) -> int:
        """获取所有上游的总排队长度（不包括正在执行的请求）"""
        return sum(queue.qsize() for queue in self._queues.values())

    def enqueue(
        self,
        *,
        request_id: str,
        user_id: int,
        tier: str,
        action: str,
        logging_config: LoggingConfig,
        estimated_cost: int,
        handler: Callable[[Any], Awaitable[bytes]],
        process_zip_response: bool = True,
        priority_override: int | None = None,
        manage_quota: bool = True,
        allowed_upstreams: frozenset[str] | set[str] | list[str] | None = None,
        accounting: RequestAccounting | None = None,
    ) -> asyncio.Future:
        if not self._accepting:
            raise QueueClosed
        if accounting is None:
            accounting = RequestAccounting(
                quota_manager=self._quota_manager,
                usage_logs=self._usage_logs,
                request_id=request_id,
                user_id=user_id,
                estimated_cost=estimated_cost,
                manage_quota=manage_quota,
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        priority = priority_override if priority_override is not None else self.VIP_PRIORITY if tier == "vip" else self.NORMAL_PRIORITY
        try:
            self._dispatch_queue.put_nowait(
                QueueItem(
                    priority=priority,
                    sequence=next(self._sequence),
                    enqueued_at=time.monotonic(),
                    request_id=request_id,
                    user_id=user_id,
                    action=action,
                    tier=tier,
                    estimated_cost=estimated_cost,
                    accounting=accounting,
                    logging_config=logging_config,
                    process_zip_response=process_zip_response,
                    handler=handler,
                    future=future,
                    allowed_upstreams=allowed_upstreams,
                )
            )
        except asyncio.QueueFull as exc:
            raise QueueFull from exc
        self._active_futures.add(future)
        future.add_done_callback(self._active_futures.discard)
        self._notify_change()
        return future

    async def submit(
        self,
        *,
        request_id: str,
        user_id: int,
        tier: str,
        action: str,
        logging_config: LoggingConfig,
        estimated_cost: int,
        handler: Callable[[Any], Awaitable[bytes]],
        process_zip_response: bool = True,
        priority_override: int | None = None,
        manage_quota: bool = True,
        allowed_upstreams: frozenset[str] | set[str] | list[str] | None = None,
        accounting: RequestAccounting | None = None,
    ) -> bytes:
        future = self.enqueue(
            request_id=request_id,
            user_id=user_id,
            tier=tier,
            action=action,
            logging_config=logging_config,
            estimated_cost=estimated_cost,
            handler=handler,
            process_zip_response=process_zip_response,
            priority_override=priority_override,
            manage_quota=manage_quota,
            allowed_upstreams=allowed_upstreams,
            accounting=accounting,
        )
        try:
            return await future
        except asyncio.CancelledError:
            future.cancel()
            raise

    async def _wait_for_active_futures(self) -> None:
        while self._active_futures:
            await asyncio.gather(*tuple(self._active_futures), return_exceptions=True)
            await asyncio.sleep(0)

    async def _run_dispatcher(self) -> None:
        while True:
            item = await self._dispatch_queue.get()
            self._dispatch_running_item = item
            self._notify_change()
            try:
                if item.future.done():
                    item.accounting.settle_failure(
                        queued_ms=int((time.monotonic() - item.enqueued_at) * 1000),
                        error_code="client_cancelled",
                        error_message="Client cancelled before dispatch",
                        attempt_number=item.attempt_number,
                    )
                    logger.info(
                        "proxy request skipped by dispatcher because caller cancelled request_id=%s attempt_number=%s",
                        item.request_id,
                        item.attempt_number,
                    )
                    continue
                if item.accounting.manage_quota and not _user_is_available(self._quota_manager, item.user_id):
                    self._finish_user_unavailable(item)
                    logger.info(
                        "proxy request skipped by dispatcher because user is unavailable request_id=%s user_id=%s attempt_number=%s",
                        item.request_id,
                        item.user_id,
                        item.attempt_number,
                    )
                    continue
                self._dispatch_to_upstream(item)
            except Exception as exc:
                if not item.future.done():
                    item.future.set_exception(exc)
            finally:
                self._dispatch_running_item = None
                self._dispatch_queue.task_done()
                self._notify_change()

    def _dispatch_to_upstream(
        self,
        item: QueueItem,
        *,
        last_429_error: APIError | None = None,
    ) -> None:
        errors = []
        last_429_error = last_429_error or item.last_429_error
        candidates = self._candidate_upstreams(item.allowed_upstreams, advance_round_robin=True)

        if not candidates:
            self._finish_unavailable_dispatch(item, errors=errors, last_429_error=last_429_error)
            return

        for upstream_id in candidates:
            queue = self._queues[upstream_id]
            try:
                upstream_future = queue.enqueue(item, allow_overflow=item.tier == "vip")
            except QueueFull as exc:
                errors.append(exc)
                continue

            upstream_future.add_done_callback(
                lambda completed,
                item=item,
                upstream_id=upstream_id,
                last_429_error=last_429_error: self._handle_upstream_completion(
                    completed,
                    item=item,
                    upstream_id=upstream_id,
                    last_429_error=last_429_error,
                )
            )
            return

        self._finish_unavailable_dispatch(item, errors=errors, last_429_error=last_429_error)

    def _handle_upstream_completion(
        self,
        completed: asyncio.Future,
        *,
        item: QueueItem,
        upstream_id: str,
        last_429_error: APIError | None,
    ) -> None:
        self._record_adaptive_result(upstream_id, completed)
        if completed.cancelled():
            if not item.future.done():
                item.future.cancel()
            return

        exc = completed.exception()
        if isinstance(exc, Retry429Error):
            if item.future.done():
                item.accounting.settle_released()
                logger.warning(
                    "proxy request 429 retry cancelled request_id=%s upstream_id=%s attempt_number=%s",
                    item.request_id,
                    upstream_id,
                    item.attempt_number,
                )
                return
            next_attempt_number = item.attempt_number + 1
            retry_error = last_429_error or exc.original_error
            if next_attempt_number >= self._retry_429_max_attempts:
                self._finish_unavailable_dispatch(item, errors=[], last_429_error=retry_error)
                return
            logger.info(
                "proxy request 429 retry attempt=%s request_id=%s upstream_id=%s next_attempt_number=%s",
                next_attempt_number,
                item.request_id,
                upstream_id,
                next_attempt_number,
            )
            try:
                self._requeue_429_retry(item, next_attempt_number=next_attempt_number, retry_error=retry_error)
            except Exception as retry_exc:
                logger.exception(
                    "proxy request 429 retry requeue failed request_id=%s next_attempt_number=%s",
                    item.request_id,
                    next_attempt_number,
                )
                if not item.future.done():
                    item.future.set_exception(retry_exc)
            return

        if item.future.done():
            return
        if exc is not None:
            item.future.set_exception(exc)
            return
        item.future.set_result(completed.result())

    def _requeue_429_retry(
        self,
        item: QueueItem,
        *,
        next_attempt_number: int,
        retry_error: APIError,
    ) -> None:
        is_vip_retry = item.tier == "vip"
        priority = self.VIP_RETRY_PRIORITY if is_vip_retry else item.priority
        next_item = dataclasses.replace(
            item,
            priority=priority,
            sequence=next(self._sequence),
            enqueued_at=time.monotonic(),
            has_retried_429=True,
            attempt_number=next_attempt_number,
            last_429_error=retry_error,
        )
        if is_vip_retry:
            self._dispatch_to_upstream(next_item, last_429_error=retry_error)
            return
        try:
            self._dispatch_queue.put_nowait(next_item)
        except asyncio.QueueFull:
            logger.warning(
                "proxy request 429 retry dispatch queue full request_id=%s next_attempt_number=%s",
                item.request_id,
                next_attempt_number,
            )
            self._finish_unavailable_dispatch(item, errors=[], last_429_error=retry_error)
        else:
            self._notify_change()

    def _finish_unavailable_dispatch(
        self,
        item: QueueItem,
        *,
        errors: list[Exception],
        last_429_error: APIError | None,
    ) -> None:
        item.accounting.settle_released()
        if last_429_error is not None:
            if not item.future.done():
                item.future.set_exception(last_429_error)
            return
        if errors:
            raise QueueFull from errors[-1]
        raise NoAvailableUpstream("No enabled upstream is available for this user")

    def _finish_user_unavailable(self, item: QueueItem) -> None:
        item.accounting.settle_rejected(
            error_code="user_unavailable",
            error_message="User is no longer active",
            log_level="INFO",
            attempt_number=item.attempt_number,
        )
        if not item.future.done():
            item.future.set_exception(UserUnavailable("User is no longer active"))

    def _candidate_upstreams(
        self,
        allowed_upstreams: frozenset[str] | set[str] | list[str] | None,
        *,
        advance_round_robin: bool,
    ) -> list[str]:
        allowed = {item for item in (allowed_upstreams or []) if item}
        candidates = [upstream_id for upstream_id in self._target_order if not allowed or upstream_id in allowed]
        if not candidates:
            return []
        if self.routing_strategy == "random":
            shuffled = list(candidates)
            random.shuffle(shuffled)
            return shuffled
        if self.routing_strategy == "adaptive_weighted_random" and advance_round_robin:
            return self._weighted_random_candidates(candidates)
        start = next(self._round_robin) if advance_round_robin else 0
        return [candidates[(start + offset) % len(candidates)] for offset in range(len(candidates))]

    def select_client(self, allowed_upstreams: frozenset[str] | set[str] | list[str] | None = None) -> Any:
        candidates = self._candidate_upstreams(allowed_upstreams, advance_round_robin=False)
        if not candidates:
            raise NoAvailableUpstream("No enabled upstream is available for this user")
        return self._targets[candidates[0]].client_provider()

    def _record_adaptive_result(self, upstream_id: str, completed: asyncio.Future) -> None:
        if self.routing_strategy != "adaptive_weighted_random" or completed.cancelled():
            return
        score = self._adaptive_scores.get(upstream_id)
        if score is None:
            return
        success_value = 0.0 if completed.exception() is not None else 1.0
        next_score = score.score * (1.0 - self._adaptive_alpha) + success_value * self._adaptive_alpha
        if next_score != score.score:
            score.score = next_score
            self._notify_change()

    def _weighted_random_candidates(self, candidates: list[str]) -> list[str]:
        remaining = list(candidates)
        ordered = []
        while remaining:
            weights = [self._adaptive_weight(upstream_id) for upstream_id in remaining]
            total_weight = sum(weights)
            if total_weight <= 0:
                random.shuffle(remaining)
                ordered.extend(remaining)
                break
            cursor = random.uniform(0, total_weight)
            running = 0.0
            selected_index = len(remaining) - 1
            for index, weight in enumerate(weights):
                running += weight
                if cursor <= running:
                    selected_index = index
                    break
            ordered.append(remaining.pop(selected_index))
        return ordered

    def _adaptive_weight(self, upstream_id: str) -> float:
        score = self._adaptive_scores.get(upstream_id)
        if score is None:
            return self._adaptive_min_weight
        return self._adaptive_min_weight + score.score

    def _notify_change(self) -> None:
        if self._on_change is not None:
            self._on_change()
