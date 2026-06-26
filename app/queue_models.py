from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from novelai_python._exceptions import APIError

from .config import LoggingConfig
from .request_accounting import RequestAccounting


class ImageHostingServiceLike(Protocol):
    max_pending_uploads: int

    async def upload_zip_images(self, *, zip_payload: bytes, request_id: str) -> list[dict[str, object]]:
        ...


@dataclass(order=True)
class QueueItem:
    """Request context shared by the dispatch queue and per-upstream queues."""

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
    is_admin_probe: bool = field(default=False, compare=False)


@dataclass(frozen=True)
class UpstreamQueueTarget:
    id: str
    client_provider: Callable[[], Any]


@dataclass
class AdaptiveUpstreamScore:
    score: float
