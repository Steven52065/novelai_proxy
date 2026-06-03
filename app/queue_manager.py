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
from .usage_logs import UsageLogRepository


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
    upstream_id: str | None = field(compare=False)
    estimated_cost: int = field(compare=False)
    manage_quota: bool = field(compare=False)
    logging_config: LoggingConfig = field(compare=False)
    process_zip_response: bool = field(compare=False)
    handler: Callable[[], Awaitable[bytes]] = field(compare=False)
    future: asyncio.Future = field(compare=False)
    is_retry_success: bool = field(default=False, compare=False)
    attempt_number: int = field(default=0, compare=False)


@dataclass(frozen=True)
class UpstreamQueueTarget:
    id: str
    client_provider: Callable[[], Any]


@dataclass
class AdaptiveUpstreamScore:
    score: float


@dataclass(order=True)
class DispatchQueueItem:
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
    allowed_upstreams: frozenset[str] | set[str] | list[str] | None = field(compare=False)
    handler: Callable[[Any], Awaitable[bytes]] = field(compare=False)
    future: asyncio.Future = field(compare=False)
    has_retried_429: bool = field(default=False, compare=False)
    attempt_number: int = field(default=0, compare=False)


class ProxyQueue:
    def __init__(
        self,
        upstream_id: str,
        quota_manager: QuotaManager,
        usage_logs: UsageLogRepository,
        max_queue_size: int,
        upstream_interval_min_seconds: float = 2,
        upstream_interval_max_seconds: float = 5,
        upstream_error_extra_delay_seconds: float = 5,
        retry_429_queue_length_threshold: int = 3,
        image_hosting: ImageHostingServiceLike | None = None,
    ):
        self.upstream_id = upstream_id
        self.quota_manager = quota_manager
        self.usage_logs = usage_logs
        self.image_hosting = image_hosting
        self.queue: asyncio.PriorityQueue[QueueItem] = asyncio.PriorityQueue(maxsize=max_queue_size)
        self.upstream_interval_min_seconds = max(0.0, float(upstream_interval_min_seconds))
        self.upstream_interval_max_seconds = max(0.0, float(upstream_interval_max_seconds))
        if self.upstream_interval_max_seconds < self.upstream_interval_min_seconds:
            raise ValueError("upstream_interval_max_seconds must be greater than or equal to upstream_interval_min_seconds")
        self.upstream_error_extra_delay_seconds = max(0.0, float(upstream_error_extra_delay_seconds))
        self.retry_429_queue_length_threshold = int(retry_429_queue_length_threshold)
        self._last_upstream_completed_at: float | None = None
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

    def enqueue(
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
        sequence_override: int | None = None,
        manage_quota: bool = True,
        is_retry_success: bool = False,
        attempt_number: int = 0,
    ) -> asyncio.Future:
        # 如果是重试（attempt_number > 0），先插入新的数据库记录
        if attempt_number > 0:
            self.usage_logs.insert_retry_attempt(
                request_id=request_id,
                attempt_number=attempt_number,
                upstream_id=self.upstream_id,
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        priority = priority_override if priority_override is not None else 0 if tier == "vip" else 10
        sequence = sequence_override if sequence_override is not None else next(self._sequence)
        item = QueueItem(
            priority=priority,
            sequence=sequence,
            enqueued_at=time.monotonic(),
            request_id=request_id,
            user_id=user_id,
            action=action,
            tier=tier,
            upstream_id=self.upstream_id,
            estimated_cost=estimated_cost,
            manage_quota=manage_quota,
            logging_config=logging_config,
            process_zip_response=process_zip_response,
            handler=handler,
            future=future,
            is_retry_success=is_retry_success,
            attempt_number=attempt_number,
        )
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull as exc:
            raise QueueFull from exc
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
        handler: Callable[[], Awaitable[bytes]],
        process_zip_response: bool = True,
        priority_override: int | None = None,
        manage_quota: bool = True,
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
        )
        return await future

    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            self._running_item = item
            self._running_started_at = time.monotonic()
            queued_ms = int((time.monotonic() - item.enqueued_at) * 1000)
            try:
                self.usage_logs.mark_running(item.request_id, queued_ms, item.upstream_id, item.attempt_number)
                logger.info(
                    "proxy request running request_id=%s upstream_id=%s queued_ms=%s attempt_number=%s",
                    item.request_id,
                    item.upstream_id,
                    queued_ms,
                    item.attempt_number,
                )
                await self._wait_for_upstream_interval(item.request_id)
                payload = await item.handler()
                # 记录请求完成时间（成功情况）
                self._last_upstream_completed_at = time.monotonic()
            except Exception as exc:
                # 记录请求完成时间（失败情况）
                self._last_upstream_completed_at = time.monotonic()

                # 检查是否为 429 错误且应该重试
                if isinstance(exc, APIError) and str(exc.code) == "429":
                    should_retry = self._should_retry_429()
                    if should_retry:
                        # 将 429 错误标记到日志，但状态仍为 failed
                        code, message = self._error_details(exc)
                        self.usage_logs.mark_failed(
                            item.request_id,
                            queued_ms=queued_ms,
                            error_code=code,
                            error_message=message,
                            attempt_number=item.attempt_number,
                        )
                        logger.warning(
                            "proxy request 429 error, will retry request_id=%s attempt_number=%s queue_size=%s threshold=%s",
                            item.request_id,
                            item.attempt_number,
                            self.queue.qsize(),
                            self.retry_429_queue_length_threshold,
                        )
                        # 429 是 API 错误，需要对下一个请求应用额外延迟
                        self._apply_error_extra_delay_next = True
                        # 抛出 Retry429Error 让调度层（RoutingProxyQueue）重新分配到其他上游
                        if not item.future.done():
                            item.future.set_exception(Retry429Error(exc))
                        # 重要：429 重试时不释放额度，因为请求还在重试中，额度应保持 reserved 状态。
                        # 如果所有上游都重试失败，调度层会在 _dispatch_to_upstream 的第 703 行统一释放额度。
                        # 注意：不在这里调用 task_done()，由 finally 块统一处理。
                        continue

                # 所有 API 错误（包括不满足重试条件的 429）都应用额外延迟
                if isinstance(exc, APIError):
                    self._apply_error_extra_delay_next = True

                # 普通错误处理：释放额度、记录日志、设置异常
                if item.manage_quota:
                    self.quota_manager.release(item.user_id, item.estimated_cost)
                code, message = self._error_details(exc)
                self.usage_logs.mark_failed(
                    item.request_id,
                    queued_ms=queued_ms,
                    error_code=code,
                    error_message=message,
                    attempt_number=item.attempt_number,
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
                self.usage_logs.mark_success(
                    item.request_id,
                    queued_ms=queued_ms,
                    final_cost=item.estimated_cost,
                    output_files=saved_files,
                    is_retry_success=item.is_retry_success,
                    attempt_number=item.attempt_number,
                )
                log_color = "white" if item.is_retry_success else "default"
                logger.info(
                    "proxy request succeeded request_id=%s final_cost=%s output_files=%s is_retry_success=%s log_color=%s",
                    item.request_id,
                    item.estimated_cost,
                    len(saved_files),
                    item.is_retry_success,
                    log_color,
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

    def _should_retry_429(self) -> bool:
        """检查当前队列状态是否允许重试 429 错误"""
        if self.retry_429_queue_length_threshold < 0:
            return False
        return self.queue.qsize() <= self.retry_429_queue_length_threshold

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
        delay = required_delay - elapsed
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
        self.usage_logs.update_image_urls(request_id, image_urls)
        logger.info("image host upload succeeded request_id=%s image_urls=%s", request_id, len(image_urls))

    @staticmethod
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


class NoAvailableUpstream(Exception):
    pass


class Retry429Error(Exception):
    """Raised when a 429 error should be retried at the routing layer."""
    def __init__(self, original_error: APIError):
        self.original_error = original_error
        super().__init__(str(original_error))


def _without_sequence(item: dict[str, object]) -> dict[str, object]:
    clean = dict(item)
    clean.pop("sequence", None)
    return clean


class RoutingProxyQueue:
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
        retry_429_queue_length_threshold: int = 3,
        image_hosting: ImageHostingServiceLike | None = None,
    ):
        self._quota_manager = quota_manager
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
        self._dispatch_queue: asyncio.PriorityQueue[DispatchQueueItem] = asyncio.PriorityQueue(maxsize=dispatch_max_queue_size)
        self._dispatch_worker: asyncio.Task | None = None
        self._dispatch_running_item: DispatchQueueItem | None = None
        self._queues = {
            target.id: ProxyQueue(
                upstream_id=target.id,
                quota_manager=quota_manager,
                usage_logs=usage_logs,
                max_queue_size=max_queue_size,
                upstream_interval_min_seconds=upstream_interval_min_seconds,
                upstream_interval_max_seconds=upstream_interval_max_seconds,
                upstream_error_extra_delay_seconds=upstream_error_extra_delay_seconds,
                retry_429_queue_length_threshold=retry_429_queue_length_threshold,
                image_hosting=image_hosting,
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

    async def stop(self) -> None:
        if self._dispatch_worker is not None:
            self._dispatch_worker.cancel()
            try:
                await self._dispatch_worker
            except asyncio.CancelledError:
                pass
        await asyncio.gather(*(queue.stop() for queue in self._queues.values()))

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
            self._dispatch_item_snapshot(item, now, position=index)
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
    ) -> bytes:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        priority = priority_override if priority_override is not None else 0 if tier == "vip" else 10
        try:
            self._dispatch_queue.put_nowait(
                DispatchQueueItem(
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
                    allowed_upstreams=allowed_upstreams,
                    handler=handler,
                    future=future,
                )
            )
        except asyncio.QueueFull as exc:
            raise QueueFull from exc
        return await future

    async def _run_dispatcher(self) -> None:
        while True:
            item = await self._dispatch_queue.get()
            self._dispatch_running_item = item
            try:
                await self._dispatch_to_upstream(item)
            except Exception as exc:
                if not item.future.done():
                    item.future.set_exception(exc)
            finally:
                self._dispatch_running_item = None
                self._dispatch_queue.task_done()

    async def _dispatch_to_upstream(self, item: DispatchQueueItem) -> None:
        errors = []
        excluded_upstreams = set()
        max_retries = 5  # 最多尝试 5 次（包括初次尝试）
        last_429_error: APIError | None = None

        for attempt in range(max_retries):
            candidates = self._candidate_upstreams(item.allowed_upstreams, advance_round_robin=(attempt == 0))
            # 排除已经返回 429 的上游
            candidates = [uid for uid in candidates if uid not in excluded_upstreams]

            if not candidates:
                break

            for upstream_id in candidates:
                target = self._targets[upstream_id]
                queue = self._queues[upstream_id]
                try:
                    upstream_future = queue.enqueue(
                        request_id=item.request_id,
                        user_id=item.user_id,
                        tier=item.tier,
                        action=item.action,
                        logging_config=item.logging_config,
                        estimated_cost=item.estimated_cost,
                        handler=lambda target=target: item.handler(target.client_provider()),
                        process_zip_response=item.process_zip_response,
                        priority_override=item.priority,
                        sequence_override=item.sequence,
                        manage_quota=item.manage_quota,
                        is_retry_success=item.has_retried_429,
                        attempt_number=item.attempt_number,
                    )
                except QueueFull as exc:
                    errors.append(exc)
                    continue

                # 设置回调记录自适应结果
                upstream_future.add_done_callback(
                    lambda completed, upstream_id=upstream_id: self._record_adaptive_result(upstream_id, completed)
                )

                # 等待上游队列执行完成
                try:
                    result = await upstream_future
                    # 成功，直接设置结果并返回
                    if not item.future.done():
                        item.future.set_result(result)
                    return
                except Retry429Error as exc:
                    # 记录这个上游返回了 429，排除它，继续尝试其他上游
                    excluded_upstreams.add(upstream_id)
                    # 标记已重试，并增加 attempt_number
                    item = dataclasses.replace(item, has_retried_429=True, attempt_number=item.attempt_number + 1)
                    if last_429_error is None:
                        last_429_error = exc.original_error
                    logger.info(
                        "proxy request 429 retry attempt=%s excluded_count=%s request_id=%s upstream_id=%s next_attempt_number=%s",
                        attempt + 1,
                        len(excluded_upstreams),
                        item.request_id,
                        upstream_id,
                        item.attempt_number,
                    )
                    # 跳出内层循环，重新选择候选上游
                    break
                except Exception as exc:
                    # 其他异常直接抛出
                    if not item.future.done():
                        item.future.set_exception(exc)
                    return
            else:
                # 所有候选上游队列都满了
                continue

            # 如果是因为 429 跳出的，继续外层循环重试
            continue

        # 所有重试都失败了
        # 如果是因为所有上游都返回 429，传播原始 429 错误
        if last_429_error is not None:
            if item.manage_quota:
                self._quota_manager.release(item.user_id, item.estimated_cost)
            raise last_429_error
        if errors:
            raise QueueFull from errors[-1]
        raise NoAvailableUpstream("No enabled upstream is available for this user")

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
        score.score = score.score * (1.0 - self._adaptive_alpha) + success_value * self._adaptive_alpha

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

    @staticmethod
    def _copy_future_result(completed: asyncio.Future, future: asyncio.Future) -> None:
        if future.done():
            return
        if completed.cancelled():
            future.cancel()
            return
        exc = completed.exception()
        if exc is not None:
            future.set_exception(exc)
            return
        future.set_result(completed.result())

    @staticmethod
    def _dispatch_item_snapshot(item: DispatchQueueItem, now: float, position: int) -> dict[str, object]:
        return {
            "request_id": item.request_id,
            "user_id": item.user_id,
            "action": item.action,
            "tier": item.tier,
            "upstream_id": None,
            "estimated_anlas_cost": item.estimated_cost,
            "priority": item.priority,
            "sequence": item.sequence,
            "position": position,
            "status": "dispatch_queued",
            "queued_seconds": max(0, int(now - item.enqueued_at)),
        }
