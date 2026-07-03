from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from novelai_python._exceptions import APIError

from ..auth import UserContext
from ..config import LoggingConfig
from ..free_small_daily_limit import (
    FreeSmallDailyLimitExceeded,
    FreeSmallDailyLimitManager,
    FreeSmallDailyReservation,
)
from ..logging_utils import json_dumps, logger
from ..queue_errors import (
    NoAvailableUpstream,
    QueueClosed,
    QueueFull,
    UpstreamExecutionTimeout,
    UserUnavailable,
)
from ..quota_manager import InsufficientQuota, QuotaManager
from ..rate_limiter import RateLimiter
from ..request_accounting import RequestAccounting
from ..routing_queue import RoutingProxyQueue
from ..usage_logs import UsageLogCreate, UsageLogRepository


MESSAGE_UPSTREAM_REQUEST_FAILED = "Upstream request failed"
MESSAGE_UPSTREAM_REQUEST_TIMED_OUT = "Upstream request timed out"
MESSAGE_PROXY_REQUEST_FAILED = "Proxy request failed"


@dataclass(frozen=True)
class ProxyTaskRequest:
    user: UserContext
    action: str
    metadata: dict[str, Any]
    request_payload: dict[str, Any]
    estimated_cost: int
    handler: Callable[[Any], Awaitable[bytes]]
    free_small_only_allowed: bool = False
    free_small_daily_count: int = 0
    process_zip_response: bool = True


@dataclass(frozen=True)
class ProxyTaskResult:
    status_code: int
    content: bytes | dict[str, Any]
    media_type: str | None = None
    headers: dict[str, str] | None = None
    request_id: str | None = None

    @property
    def is_binary(self) -> bool:
        return isinstance(self.content, bytes)


@dataclass(frozen=True)
class PreQueueCheck:
    rejected: ProxyTaskResult | None = None
    free_small_daily_reservation: FreeSmallDailyReservation | None = None


class ProxyRequestService:
    def __init__(
        self,
        *,
        rate_limiter: RateLimiter,
        quota_manager: QuotaManager,
        free_small_daily_limit_manager: FreeSmallDailyLimitManager | None = None,
        proxy_queue: RoutingProxyQueue,
        usage_logs: UsageLogRepository,
        logging_config: LoggingConfig,
    ):
        self.rate_limiter = rate_limiter
        self.quota_manager = quota_manager
        self.free_small_daily_limit_manager = free_small_daily_limit_manager
        self.proxy_queue = proxy_queue
        self.usage_logs = usage_logs
        self.logging_config = logging_config

    async def submit_zip(self, task: ProxyTaskRequest) -> ProxyTaskResult:
        return await self.submit_binary(
            task,
            media_type="application/zip",
            response_headers={"Content-Disposition": "attachment;filename=image.zip"},
        )

    async def submit_binary(
        self,
        task: ProxyTaskRequest,
        *,
        media_type: str,
        response_headers: dict[str, str] | None = None,
    ) -> ProxyTaskResult:
        request_id = uuid.uuid4().hex
        logger.debug(
            "proxy request received request_id=%s user_id=%s action=%s payload=%s",
            request_id,
            task.user.id,
            task.action,
            json_dumps(task.request_payload),
        )

        pre_queue = self._check_before_queue(request_id, task)
        if pre_queue.rejected is not None:
            return pre_queue.rejected
        # 预检通过后额度与每日预约都已 reserved，
        # 之后的释放 / 确认 / 日志终态统一由记账生命周期对象负责。
        accounting = RequestAccounting(
            quota_manager=self.quota_manager,
            usage_logs=self.usage_logs,
            request_id=request_id,
            user_id=task.user.id,
            estimated_cost=task.estimated_cost,
            free_small_daily_limit_manager=self.free_small_daily_limit_manager,
            free_small_daily_reservation=pre_queue.free_small_daily_reservation,
        )

        try:
            self._insert_queued(
                request_id,
                task,
                error_code=None,
                error_message=None,
                log_level="INFO",
            )
        except Exception:
            accounting.settle_released()
            raise
        logger.info(
            "proxy request queued request_id=%s user_id=%s action=%s estimated_cost=%s",
            request_id,
            task.user.id,
            task.action,
            task.estimated_cost,
        )

        try:
            queue_future = self.proxy_queue.enqueue(
                request_id=request_id,
                user_id=task.user.id,
                tier=task.user.tier,
                action=task.action,
                logging_config=self.logging_config,
                estimated_cost=task.estimated_cost,
                handler=task.handler,
                process_zip_response=task.process_zip_response,
                allowed_upstreams=task.user.allowed_upstreams,
                accounting=accounting,
            )
        except QueueClosed:
            accounting.settle_rejected(
                error_code="server_shutting_down",
                error_message="Server is shutting down, please retry later",
                log_level="INFO",
            )
            logger.info("proxy queue closed during shutdown request_id=%s user_id=%s", request_id, task.user.id)
            return ProxyTaskResult(
                status_code=503,
                content={"message": "Server is shutting down, please retry later"},
                request_id=request_id,
            )
        except QueueFull:
            accounting.settle_rejected(
                error_code="queue_full",
                error_message="Queue full, please retry later",
                log_level="ERROR",
            )
            logger.error("proxy queue full request_id=%s user_id=%s", request_id, task.user.id)
            return ProxyTaskResult(
                status_code=503,
                content={"message": "Queue full, please retry later"},
                request_id=request_id,
            )

        try:
            binary_payload = await queue_future
        except QueueFull:
            accounting.settle_rejected(
                error_code="queue_full",
                error_message="Queue full, please retry later",
                log_level="ERROR",
            )
            logger.error("proxy queue full request_id=%s user_id=%s", request_id, task.user.id)
            return ProxyTaskResult(
                status_code=503,
                content={"message": "Queue full, please retry later"},
                request_id=request_id,
            )
        except NoAvailableUpstream as exc:
            accounting.settle_rejected(
                error_code="no_available_upstream",
                error_message=str(exc),
                log_level="ERROR",
            )
            logger.error("no available upstream request_id=%s user_id=%s", request_id, task.user.id)
            return ProxyTaskResult(
                status_code=503,
                content={"message": "No enabled upstream is available for this user"},
                request_id=request_id,
            )
        except UserUnavailable as exc:
            logger.info("proxy request rejected because user is unavailable request_id=%s user_id=%s", request_id, task.user.id)
            return ProxyTaskResult(
                status_code=403,
                content={"message": str(exc)},
                request_id=request_id,
            )
        except APIError as exc:
            status_code = int(exc.code) if str(exc.code or "").isdigit() else 502
            logger.error("upstream API error request_id=%s code=%s message=%s", request_id, exc.code, exc.message)
            return ProxyTaskResult(
                status_code=status_code,
                content={"message": MESSAGE_UPSTREAM_REQUEST_FAILED},
                request_id=request_id,
            )
        except UpstreamExecutionTimeout:
            logger.error("upstream execution timed out request_id=%s", request_id)
            return ProxyTaskResult(
                status_code=504,
                content={"message": MESSAGE_UPSTREAM_REQUEST_TIMED_OUT},
                request_id=request_id,
            )
        except asyncio.CancelledError:
            queue_future.cancel()
            raise
        except Exception as exc:
            logger.exception("proxy request failed request_id=%s", request_id)
            return ProxyTaskResult(status_code=502, content={"message": MESSAGE_PROXY_REQUEST_FAILED}, request_id=request_id)

        return ProxyTaskResult(
            status_code=201,
            content=binary_payload,
            media_type=media_type,
            headers=response_headers,
            request_id=request_id,
        )

    def _check_before_queue(self, request_id: str, task: ProxyTaskRequest) -> PreQueueCheck:
        # These checks happen before queue submission so rejected requests never reserve upstream capacity.
        if task.user.free_small_only and not task.free_small_only_allowed:
            self._insert_rejected(
                request_id,
                task,
                error_code="free_small_only_blocked",
                error_message="User is limited to definitely free small image generations",
                log_level="INFO",
            )
            logger.info(
                "proxy request rejected by free small only request_id=%s user_id=%s action=%s estimated_cost=%s",
                request_id,
                task.user.id,
                task.action,
                task.estimated_cost,
            )
            return PreQueueCheck(
                rejected=ProxyTaskResult(
                    status_code=403,
                    content={"message": "User is limited to definitely free small image generations"},
                    request_id=request_id,
                )
            )

        rate = self.rate_limiter.check(task.user.id)
        if not rate.allowed:
            self._insert_rejected(
                request_id,
                task,
                estimated_cost=0,
                error_code="rate_limited",
                error_message=rate.message,
                log_level="INFO",
            )
            logger.info("proxy request rate limited request_id=%s user_id=%s", request_id, task.user.id)
            return PreQueueCheck(
                rejected=ProxyTaskResult(
                    status_code=429,
                    content={"message": rate.message, "retry_after": rate.retry_after, "limit_scope": rate.scope},
                    headers={"Retry-After": str(rate.retry_after)},
                    request_id=request_id,
                )
            )

        try:
            free_small_daily_reservation = self._reserve_free_small_daily_limit(request_id, task)
        except FreeSmallDailyLimitExceeded as exc:
            self._insert_rejected(
                request_id,
                task,
                estimated_cost=0,
                error_code="free_small_daily_limit_exceeded",
                error_message=str(exc),
                log_level="INFO",
            )
            snapshot = exc.snapshot
            logger.info(
                "proxy request rejected by free small daily limit request_id=%s user_id=%s scope=%s limit=%s requested=%s",
                request_id,
                task.user.id,
                snapshot.scope,
                snapshot.limit,
                exc.requested,
            )
            return PreQueueCheck(
                rejected=ProxyTaskResult(
                    status_code=429,
                    content={
                        "message": str(exc),
                        "retry_after": exc.retry_after,
                        "limit_scope": snapshot.scope,
                        "limit": snapshot.limit,
                        "used": snapshot.used,
                        "reserved": snapshot.reserved,
                        "requested": exc.requested,
                        "remaining": exc.remaining,
                    },
                    headers={"Retry-After": str(exc.retry_after)},
                    request_id=request_id,
                )
            )

        if task.estimated_cost < 0:
            self._release_free_small_daily_reservation(free_small_daily_reservation)
            self._insert_rejected(
                request_id,
                task,
                error_code="unsupported_cost",
                error_message="Request exceeds supported cost bounds",
                log_level="ERROR",
            )
            logger.error("proxy request has unsupported cost request_id=%s estimated_cost=%s", request_id, task.estimated_cost)
            return PreQueueCheck(
                rejected=ProxyTaskResult(
                    status_code=400,
                    content={"message": "Request exceeds supported cost bounds"},
                    request_id=request_id,
                )
            )

        try:
            self.quota_manager.reserve(task.user.id, task.estimated_cost)
        except InsufficientQuota as exc:
            self._release_free_small_daily_reservation(free_small_daily_reservation)
            self._insert_rejected(
                request_id,
                task,
                error_code="insufficient_anlas",
                error_message=str(exc),
                log_level="INFO",
            )
            logger.info(
                "proxy request rejected for quota request_id=%s user_id=%s need=%s have=%s",
                request_id,
                task.user.id,
                exc.need,
                exc.have,
            )
            return PreQueueCheck(
                rejected=ProxyTaskResult(
                    status_code=402,
                    content={"message": str(exc), "need": exc.need, "have": exc.have},
                    request_id=request_id,
                )
            )

        return PreQueueCheck(free_small_daily_reservation=free_small_daily_reservation)

    def _reserve_free_small_daily_limit(
        self,
        request_id: str,
        task: ProxyTaskRequest,
    ) -> FreeSmallDailyReservation | None:
        if self.free_small_daily_limit_manager is None:
            return None
        if not task.free_small_only_allowed:
            return None
        count = int(task.free_small_daily_count or 0)
        if count <= 0:
            return None
        reservation = self.free_small_daily_limit_manager.reserve(task.user.id, count)
        if reservation is not None:
            logger.info(
                "free small daily limit reserved request_id=%s user_id=%s count=%s scope=%s limit=%s",
                request_id,
                task.user.id,
                count,
                reservation.scope,
                reservation.limit,
            )
        return reservation

    def _release_free_small_daily_reservation(self, reservation: FreeSmallDailyReservation | None) -> None:
        if reservation is None or self.free_small_daily_limit_manager is None:
            return
        try:
            self.free_small_daily_limit_manager.release(reservation)
        except Exception:
            logger.exception("failed to release free small daily reservation user_id=%s", reservation.user_id)

    def _insert_queued(
        self,
        request_id: str,
        task: ProxyTaskRequest,
        *,
        error_code: str | None,
        error_message: str | None,
        log_level: str,
        estimated_cost: int | None = None,
    ) -> None:
        self.usage_logs.insert_queued(
            self._usage_log_create(
                request_id,
                task,
                error_code=error_code,
                error_message=error_message,
                log_level=log_level,
                estimated_cost=estimated_cost,
            )
        )

    def _insert_rejected(
        self,
        request_id: str,
        task: ProxyTaskRequest,
        *,
        error_code: str | None,
        error_message: str | None,
        log_level: str,
        estimated_cost: int | None = None,
    ) -> None:
        self.usage_logs.insert_rejected(
            self._usage_log_create(
                request_id,
                task,
                error_code=error_code,
                error_message=error_message,
                log_level=log_level,
                estimated_cost=estimated_cost,
            )
        )

    def _usage_log_create(
        self,
        request_id: str,
        task: ProxyTaskRequest,
        *,
        error_code: str | None,
        error_message: str | None,
        log_level: str,
        estimated_cost: int | None,
    ) -> UsageLogCreate:
        return UsageLogCreate(
            request_id=request_id,
            user_id=task.user.id,
            action=task.action,
            model=task.metadata.get("model"),
            width=_optional_int(task.metadata.get("width")),
            height=_optional_int(task.metadata.get("height")),
            steps=_optional_int(task.metadata.get("steps")),
            n_samples=_optional_int(task.metadata.get("n_samples")),
            estimated_anlas_cost=int(task.estimated_cost if estimated_cost is None else estimated_cost),
            error_code=error_code,
            error_message=error_message,
            log_level=log_level,
            request_payload=task.request_payload,
        )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
