from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from novelai_python._exceptions import APIError

from ..auth import UserContext
from ..config import LoggingConfig
from ..logging_utils import json_dumps, logger
from ..queue_manager import ProxyQueue, QueueFull
from ..quota_manager import InsufficientQuota, QuotaManager
from ..rate_limiter import RateLimiter
from ..usage_logs import UsageLogCreate, UsageLogRepository


@dataclass(frozen=True)
class ProxyTaskRequest:
    user: UserContext
    action: str
    metadata: dict[str, Any]
    request_payload: dict[str, Any]
    estimated_cost: int
    handler: Callable[[], Awaitable[bytes]]
    free_small_only_allowed: bool = False
    process_zip_response: bool = True


@dataclass(frozen=True)
class ProxyTaskResult:
    status_code: int
    content: bytes | dict[str, Any]
    media_type: str | None = None
    headers: dict[str, str] | None = None

    @property
    def is_binary(self) -> bool:
        return isinstance(self.content, bytes)


class ProxyRequestService:
    def __init__(
        self,
        *,
        rate_limiter: RateLimiter,
        quota_manager: QuotaManager,
        proxy_queue: ProxyQueue,
        usage_logs: UsageLogRepository,
        logging_config: LoggingConfig,
    ):
        self.rate_limiter = rate_limiter
        self.quota_manager = quota_manager
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

        rejected = self._reject_before_queue(request_id, task)
        if rejected is not None:
            return rejected

        self._insert_queued(
            request_id,
            task,
            error_code=None,
            error_message=None,
            log_level="INFO",
        )
        logger.info(
            "proxy request queued request_id=%s user_id=%s action=%s estimated_cost=%s",
            request_id,
            task.user.id,
            task.action,
            task.estimated_cost,
        )

        try:
            binary_payload = await self.proxy_queue.submit(
                request_id=request_id,
                user_id=task.user.id,
                tier=task.user.tier,
                action=task.action,
                logging_config=self.logging_config,
                estimated_cost=task.estimated_cost,
                handler=task.handler,
                process_zip_response=task.process_zip_response,
            )
        except QueueFull:
            self.quota_manager.release(task.user.id, task.estimated_cost)
            self.usage_logs.mark_rejected(
                request_id,
                error_code="queue_full",
                error_message="Queue full, please retry later",
                log_level="ERROR",
            )
            logger.error("proxy queue full request_id=%s user_id=%s", request_id, task.user.id)
            return ProxyTaskResult(
                status_code=503,
                content={"message": "Queue full, please retry later"},
            )
        except APIError as exc:
            status_code = int(exc.code) if str(exc.code or "").isdigit() else 502
            logger.error("upstream API error request_id=%s code=%s message=%s", request_id, exc.code, exc.message)
            return ProxyTaskResult(
                status_code=status_code,
                content=exc.response if isinstance(exc.response, dict) else {"message": exc.message},
            )
        except Exception as exc:
            logger.exception("proxy request failed request_id=%s", request_id)
            return ProxyTaskResult(status_code=502, content={"message": str(exc)})

        return ProxyTaskResult(
            status_code=201,
            content=binary_payload,
            media_type=media_type,
            headers=response_headers,
        )

    def _reject_before_queue(self, request_id: str, task: ProxyTaskRequest) -> ProxyTaskResult | None:
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
            return ProxyTaskResult(
                status_code=403,
                content={"message": "User is limited to definitely free small image generations"},
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
            return ProxyTaskResult(
                status_code=429,
                content={"message": rate.message, "retry_after": rate.retry_after},
                headers={"Retry-After": str(rate.retry_after)},
            )

        if task.estimated_cost < 0:
            self._insert_rejected(
                request_id,
                task,
                error_code="unsupported_cost",
                error_message="Request exceeds supported cost bounds",
                log_level="ERROR",
            )
            logger.error("proxy request has unsupported cost request_id=%s estimated_cost=%s", request_id, task.estimated_cost)
            return ProxyTaskResult(
                status_code=400,
                content={"message": "Request exceeds supported cost bounds"},
            )

        try:
            self.quota_manager.reserve(task.user.id, task.estimated_cost)
        except InsufficientQuota as exc:
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
            return ProxyTaskResult(
                status_code=402,
                content={"message": str(exc), "need": exc.need, "have": exc.have},
            )

        return None

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
