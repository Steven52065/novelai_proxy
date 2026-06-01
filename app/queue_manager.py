from __future__ import annotations

import asyncio
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


@dataclass(frozen=True)
class UpstreamQueueTarget:
    id: str
    client_provider: Callable[[], Any]


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
    ) -> asyncio.Future:
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
                self.usage_logs.mark_running(item.request_id, queued_ms, item.upstream_id)
                logger.info(
                    "proxy request running request_id=%s upstream_id=%s queued_ms=%s",
                    item.request_id,
                    item.upstream_id,
                    queued_ms,
                )
                await self._wait_for_upstream_interval(item.request_id)
                payload = await item.handler()
            except Exception as exc:
                if isinstance(exc, APIError):
                    self._apply_error_extra_delay_next = True
                if item.manage_quota:
                    self.quota_manager.release(item.user_id, item.estimated_cost)
                code, message = self._error_details(exc)
                self.usage_logs.mark_failed(
                    item.request_id,
                    queued_ms=queued_ms,
                    error_code=code,
                    error_message=message,
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
        routing_strategy: Literal["round_robin", "random"] = "round_robin",
        upstream_interval_min_seconds: float = 2,
        upstream_interval_max_seconds: float = 5,
        upstream_error_extra_delay_seconds: float = 5,
        image_hosting: ImageHostingServiceLike | None = None,
    ):
        enabled_targets = [target for target in targets if target.id]
        if not enabled_targets:
            raise ValueError("at least one upstream target is required")
        self.routing_strategy = routing_strategy
        self._image_hosting = image_hosting
        self._targets = {target.id: target for target in enabled_targets}
        self._target_order = [target.id for target in enabled_targets]
        self._round_robin = itertools.count()
        self._sequence = itertools.count()
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
                self._dispatch_to_upstream(item)
            except Exception as exc:
                if not item.future.done():
                    item.future.set_exception(exc)
            finally:
                self._dispatch_running_item = None
                self._dispatch_queue.task_done()

    def _dispatch_to_upstream(self, item: DispatchQueueItem) -> None:
        errors = []
        for upstream_id in self._candidate_upstreams(item.allowed_upstreams, advance_round_robin=True):
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
                )
            except QueueFull as exc:
                errors.append(exc)
                continue
            upstream_future.add_done_callback(lambda completed, future=item.future: self._copy_future_result(completed, future))
            return
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
        start = next(self._round_robin) if advance_round_robin else 0
        return [candidates[(start + offset) % len(candidates)] for offset in range(len(candidates))]

    def select_client(self, allowed_upstreams: frozenset[str] | set[str] | list[str] | None = None) -> Any:
        candidates = self._candidate_upstreams(allowed_upstreams, advance_round_robin=False)
        if not candidates:
            raise NoAvailableUpstream("No enabled upstream is available for this user")
        return self._targets[candidates[0]].client_provider()

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
