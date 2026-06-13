from __future__ import annotations

import asyncio

from novelai_python._exceptions import APIError


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
