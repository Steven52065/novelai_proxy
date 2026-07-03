from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import itertools
import random
import time
from typing import Any, Awaitable, Callable, Literal

from novelai_python._exceptions import APIError

from .config import LoggingConfig
from .logging_utils import logger
from .quota_manager import QuotaManager
from .queue_errors import NoAvailableUpstream, QueueClosed, QueueFull, Retry429Error, UpstreamItemRerouted, UserUnavailable
from .queue_models import AdaptiveUpstreamScore, ImageHostingServiceLike, QueueItem, UpstreamQueueTarget
from .queue_snapshot import RoutingQueueSnapshot
from .queue_snapshot_helpers import item_snapshot, without_sequence
from .queue_tiers import (
    QUEUE_PRIORITY_NORMAL,
    QUEUE_PRIORITY_VIP,
    QUEUE_PRIORITY_VIP_RETRY,
    priority_for_tier,
    tier_allows_overflow,
)
from .queue_work_queue import PriorityWorkQueue
from .request_accounting import NoopRequestAccounting, RequestAccounting
from .retry_policy import RetryDecision, RetryPolicy
from .upstream_queue import ProxyQueue
from .usage_logs import UsageLogRepository


class RoutingProxyQueue:
    VIP_PRIORITY = QUEUE_PRIORITY_VIP
    NORMAL_PRIORITY = QUEUE_PRIORITY_NORMAL
    VIP_RETRY_PRIORITY = QUEUE_PRIORITY_VIP_RETRY

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
        is_user_available: Callable[[int], bool] | None = None,
        image_hosting: ImageHostingServiceLike | None = None,
        on_change: Callable[[], None] | None = None,
        on_upstream_api_error: Callable[[str, APIError], None] | None = None,
    ):
        self._quota_manager = quota_manager
        self._usage_logs = usage_logs
        self._retry_policy = RetryPolicy(
            queue_length_threshold=retry_429_queue_length_threshold,
            max_attempts=retry_429_max_attempts,
            vip_retry_priority=self.VIP_RETRY_PRIORITY,
        )
        enabled_targets = [target for target in targets if target.id]
        self.routing_strategy = routing_strategy
        self._image_hosting = image_hosting
        self._max_queue_size = max_queue_size
        self._upstream_interval_min_seconds = upstream_interval_min_seconds
        self._upstream_interval_max_seconds = upstream_interval_max_seconds
        self._upstream_error_extra_delay_seconds = upstream_error_extra_delay_seconds
        self._upstream_execution_timeout_seconds = upstream_execution_timeout_seconds
        self._targets = {target.id: target for target in enabled_targets}
        self._target_order = [target.id for target in enabled_targets]
        self._round_robin = itertools.count()
        self._sequence = itertools.count()
        self._adaptive_alpha = max(0.0, min(1.0, float(adaptive_alpha)))
        self._adaptive_min_weight = max(0.0, float(adaptive_min_weight))
        initial_score = max(0.0, min(1.0, float(adaptive_initial_score)))
        self._adaptive_initial_score = initial_score
        self._adaptive_scores = {
            target.id: AdaptiveUpstreamScore(score=initial_score)
            for target in enabled_targets
        }
        if dispatch_max_queue_size is None:
            dispatch_max_queue_size = max_queue_size * max(len(enabled_targets), 1)
        self._started = False
        self._dispatch_queue = PriorityWorkQueue(maxsize=dispatch_max_queue_size)
        self._dispatch_worker: asyncio.Task | None = None
        self._dispatch_running_item: QueueItem | None = None
        self._removed_queue_stop_tasks: set[asyncio.Task] = set()
        self._removed_queue_stop_futures: set[concurrent.futures.Future] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._accepting = True
        self._active_futures: set[asyncio.Future] = set()
        self._on_change = on_change
        self._on_upstream_api_error = on_upstream_api_error
        self._is_user_available = is_user_available or (lambda _user_id: True)
        self._queues = {
            target.id: self._create_upstream_queue(target)
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
        self._started = True
        self._loop = asyncio.get_running_loop()
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
        stop_tasks = [queue.stop(drain=drain) for queue in self._queues.values()]
        if self._removed_queue_stop_tasks:
            stop_tasks.extend(asyncio.shield(task) for task in tuple(self._removed_queue_stop_tasks))
        if self._removed_queue_stop_futures:
            stop_tasks.extend(asyncio.wrap_future(future) for future in tuple(self._removed_queue_stop_futures))
        if stop_tasks:
            await asyncio.gather(*stop_tasks)
        self._started = False

    def sync_targets(self, targets: list[UpstreamQueueTarget]) -> None:
        previous_target_order = list(self._target_order)
        enabled_targets = [target for target in targets if target.id]
        new_target_ids = [target.id for target in enabled_targets]
        self._targets = {target.id: target for target in enabled_targets}
        self._target_order = new_target_ids
        removed_target_ids = [upstream_id for upstream_id in previous_target_order if upstream_id not in self._targets]
        for target in enabled_targets:
            if target.id not in self._queues:
                queue = self._create_upstream_queue(target)
                self._queues[target.id] = queue
                if self._started:
                    queue.start()
            else:
                self._queues[target.id].client_provider = target.client_provider
                self._queues[target.id].on_api_error = self._on_upstream_api_error
            self._adaptive_scores.setdefault(target.id, AdaptiveUpstreamScore(score=self._adaptive_initial_score))

        for upstream_id in removed_target_ids:
            self._reroute_pending_from_disabled_upstream(upstream_id)
            queue = self._queues.pop(upstream_id, None)
            if queue is not None:
                self._track_removed_queue_stop(queue)

        self._notify_change()

    async def wait_for_image_uploads(self) -> None:
        await asyncio.gather(*(queue.wait_for_image_uploads() for queue in self._queues.values()))

    def qsize(self) -> int:
        return self._dispatch_queue.qsize() + sum(
            self._queues[upstream_id].qsize()
            for upstream_id in self._target_order
            if upstream_id in self._queues
        )

    def snapshot(self) -> RoutingQueueSnapshot:
        upstream_snapshots = []
        flattened_running = []
        flattened_queued = []
        now = time.monotonic()
        dispatch_queued = [
            item_snapshot(item, now, "dispatch_queued", position=index)
            for index, item in enumerate(self._dispatch_queue.snapshot_items(), start=1)
        ]
        for upstream_id in self._target_order:
            upstream_snapshot = self._queues[upstream_id].snapshot()
            upstream_snapshot = {"id": upstream_id, **upstream_snapshot}
            if upstream_snapshot["running"] is not None:
                upstream_snapshot["running"].pop("sequence", None)
                flattened_running.append(upstream_snapshot["running"])
            for item in upstream_snapshot["queued"]:
                item["upstream_position"] = item["position"]
            flattened_queued.extend(upstream_snapshot["queued"])
            upstream_snapshot["queued"] = [without_sequence(item) for item in upstream_snapshot["queued"]]
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
                "running": self._queues[upstream_id].running_item is not None,
            })
        return {
            "strategy": self.routing_strategy,
            "upstreams": upstreams,
        }

    def _create_upstream_queue(self, target: UpstreamQueueTarget) -> ProxyQueue:
        return ProxyQueue(
            upstream_id=target.id,
            usage_logs=self._usage_logs,
            max_queue_size=self._max_queue_size,
            client_provider=target.client_provider,
            upstream_interval_min_seconds=self._upstream_interval_min_seconds,
            upstream_interval_max_seconds=self._upstream_interval_max_seconds,
            upstream_error_extra_delay_seconds=self._upstream_error_extra_delay_seconds,
            upstream_execution_timeout_seconds=self._upstream_execution_timeout_seconds,
            retry_policy=self._retry_policy,
            get_total_queue_length=self._get_total_queue_length,
            is_user_available=self._is_user_available,
            image_hosting=self._image_hosting,
            on_change=self._on_change,
            on_api_error=self._on_upstream_api_error,
        )

    def _track_removed_queue_stop(self, queue: ProxyQueue) -> None:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is not None:
            task = running_loop.create_task(queue.stop(drain=True))
            self._removed_queue_stop_tasks.add(task)
            task.add_done_callback(self._discard_removed_queue_stop_task)
            return
        if self._loop is not None and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(queue.stop(drain=True), self._loop)
            self._removed_queue_stop_futures.add(future)
            future.add_done_callback(self._discard_removed_queue_stop_future)

    def _discard_removed_queue_stop_task(self, task: asyncio.Task) -> None:
        self._removed_queue_stop_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _discard_removed_queue_stop_future(self, future: concurrent.futures.Future) -> None:
        self._removed_queue_stop_futures.discard(future)
        if not future.cancelled():
            future.exception()

    def _get_total_queue_length(self) -> int:
        return sum(
            self._queues[upstream_id].qsize()
            for upstream_id in self._target_order
            if upstream_id in self._queues
        )

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
        priority = priority_override if priority_override is not None else priority_for_tier(tier)
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

    async def submit_upstream_probe(
        self,
        *,
        upstream_id: str,
        request_id: str,
        logging_config: LoggingConfig,
        handler: Callable[[Any], Awaitable[bytes]],
    ) -> bytes:
        if not self._accepting:
            raise QueueClosed
        if upstream_id not in self._targets:
            raise NoAvailableUpstream(f"Unknown upstream id: {upstream_id}")
        queue = self._queues.get(upstream_id)
        if queue is None:
            raise NoAvailableUpstream(f"Unknown upstream id: {upstream_id}")

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        item = QueueItem(
            priority=QUEUE_PRIORITY_NORMAL,
            sequence=next(self._sequence),
            enqueued_at=time.monotonic(),
            request_id=request_id,
            user_id=0,
            action="admin-upstream-test",
            tier="admin",
            estimated_cost=0,
            accounting=NoopRequestAccounting(),
            logging_config=logging_config,
            process_zip_response=False,
            handler=handler,
            future=future,
            allowed_upstreams=frozenset({upstream_id}),
            is_admin_probe=True,
        )
        upstream_future = queue.enqueue(item, allow_overflow=False)
        self._active_futures.add(future)
        future.add_done_callback(self._active_futures.discard)
        upstream_future.add_done_callback(
            lambda completed, future=future: self._handle_probe_completion(completed, future=future)
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
                if item.accounting.manage_quota and not self._is_user_available(item.user_id):
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
                upstream_future = queue.enqueue(item, allow_overflow=tier_allows_overflow(item.tier))
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
        if not completed.cancelled():
            exc = completed.exception()
            if isinstance(exc, UpstreamItemRerouted):
                return
        else:
            exc = None
        if not item.is_admin_probe:
            self._record_adaptive_result(upstream_id, completed)
        if completed.cancelled():
            if not item.future.done():
                item.future.cancel()
            return

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
            retry_error = last_429_error or exc.original_error
            decision = self._retry_policy.decide_retry(
                tier=item.tier,
                attempt_number=item.attempt_number,
                current_priority=item.priority,
            )
            if not decision.should_retry:
                self._finish_unavailable_dispatch(item, errors=[], last_429_error=retry_error)
                return
            logger.info(
                "proxy request 429 retry attempt=%s request_id=%s upstream_id=%s next_attempt_number=%s",
                decision.next_attempt_number,
                item.request_id,
                upstream_id,
                decision.next_attempt_number,
            )
            try:
                self._requeue_429_retry(item, decision=decision, retry_error=retry_error)
            except Exception as retry_exc:
                logger.exception(
                    "proxy request 429 retry requeue failed request_id=%s next_attempt_number=%s",
                    item.request_id,
                    decision.next_attempt_number,
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

    def _handle_probe_completion(self, completed: asyncio.Future, *, future: asyncio.Future) -> None:
        if future.done():
            if not completed.cancelled():
                completed.exception()
            return
        if completed.cancelled():
            future.cancel()
            return
        exc = completed.exception()
        if exc is not None:
            future.set_exception(exc)
            return
        future.set_result(completed.result())

    def _requeue_429_retry(
        self,
        item: QueueItem,
        *,
        decision: RetryDecision,
        retry_error: APIError,
    ) -> None:
        next_item = dataclasses.replace(
            item,
            priority=decision.priority,
            sequence=next(self._sequence),
            enqueued_at=time.monotonic(),
            has_retried_429=True,
            attempt_number=decision.next_attempt_number,
            last_429_error=retry_error,
        )
        if decision.immediate:
            self._dispatch_to_upstream(next_item, last_429_error=retry_error)
            return
        try:
            self._dispatch_queue.put_nowait(next_item)
        except asyncio.QueueFull:
            logger.warning(
                "proxy request 429 retry dispatch queue full request_id=%s next_attempt_number=%s",
                item.request_id,
                decision.next_attempt_number,
            )
            self._finish_unavailable_dispatch(item, errors=[], last_429_error=retry_error)
        else:
            self._notify_change()

    def _reroute_pending_from_disabled_upstream(self, upstream_id: str) -> None:
        queue = self._queues.get(upstream_id)
        if queue is None:
            return
        pending_items = queue.extract_pending_items()
        if not pending_items:
            return

        rerouted = 0
        failed = 0
        for item in pending_items:
            if item.is_admin_probe:
                self._finish_disabled_probe(item, upstream_id=upstream_id)
                failed += 1
                continue
            if item.future.done():
                continue
            original_future = item.cancel_future
            if original_future is None:
                self._settle_routing_rejection(
                    item,
                    error_code="no_available_upstream",
                    error_message=f"Upstream is unavailable: {upstream_id}",
                )
                item.future.set_exception(NoAvailableUpstream(f"Upstream is unavailable: {upstream_id}"))
                failed += 1
                continue

            item.future.set_exception(UpstreamItemRerouted())
            if original_future.done():
                item.accounting.settle_failure(
                    queued_ms=int((time.monotonic() - item.enqueued_at) * 1000),
                    error_code="client_cancelled",
                    error_message="Client cancelled before upstream reroute",
                    attempt_number=item.attempt_number,
                )
                failed += 1
                continue

            dispatch_item = dataclasses.replace(
                item,
                sequence=next(self._sequence),
                enqueued_at=time.monotonic(),
                upstream_id=None,
                future=original_future,
                cancel_future=None,
            )
            try:
                self._dispatch_queue.put_nowait(dispatch_item)
            except asyncio.QueueFull:
                self._settle_routing_rejection(
                    item,
                    error_code="queue_full",
                    error_message="Queue full, please retry later",
                )
                if not original_future.done():
                    original_future.set_exception(QueueFull())
                failed += 1
            else:
                rerouted += 1

        logger.info(
            "disabled upstream pending queue processed upstream_id=%s rerouted=%s failed=%s",
            upstream_id,
            rerouted,
            failed,
        )
        self._notify_change()

    @staticmethod
    def _finish_disabled_probe(item: QueueItem, *, upstream_id: str) -> None:
        if not item.future.done():
            item.future.set_exception(NoAvailableUpstream(f"Upstream is unavailable: {upstream_id}"))

    def _finish_unavailable_dispatch(
        self,
        item: QueueItem,
        *,
        errors: list[Exception],
        last_429_error: APIError | None,
    ) -> None:
        if errors:
            self._settle_routing_rejection(
                item,
                error_code="queue_full",
                error_message="Queue full, please retry later",
            )
        else:
            self._settle_routing_rejection(
                item,
                error_code="no_available_upstream",
                error_message="No enabled upstream is available for this user",
            )
        if last_429_error is not None:
            if not item.future.done():
                item.future.set_exception(last_429_error)
            return
        if errors:
            raise QueueFull from errors[-1]
        raise NoAvailableUpstream("No enabled upstream is available for this user")

    @staticmethod
    def _settle_routing_rejection(
        item: QueueItem,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        if item.attempt_number > 0 and item.retry_attempt_logged:
            item.accounting.settle_rejected(
                error_code=error_code,
                error_message=error_message,
                log_level="ERROR",
                attempt_number=item.attempt_number,
            )
            return
        item.accounting.settle_released()

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
