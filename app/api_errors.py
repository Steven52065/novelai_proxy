from __future__ import annotations

from typing import Any


class NovelAIProxyError(Exception):
    """Base exception for errors returned by the NovelAI upstream."""

    message: str

    def __init__(self, message: str) -> None:
        # Keep the former exception contract's BaseException.args behavior. The
        # concrete API errors are constructed with four positional arguments,
        # and existing logs may rely on that representation.
        self.message = message

    @property
    def __dict__(self) -> dict[str, Any]:
        return {"message": self.message}


class APIError(NovelAIProxyError):
    """Raised when the NovelAI API returns an error response."""

    request: Any
    code: str | None = None
    response: dict[str, Any] | str | None = None

    def __init__(
        self,
        message: str,
        request: Any,
        response: dict[str, Any] | str,
        code: str | None,
    ) -> None:
        super().__init__(message)
        self.request = request
        self.response = response
        self.code = code

    @property
    def __dict__(self) -> dict[str, Any]:
        values = super().__dict__
        values.update(
            {
                "request": self.request,
                "response": self.response,
                "code": self.code,
            }
        )
        return values


class AuthError(APIError):
    """Raised when the upstream rejects the configured credential."""


class DataSerializationError(APIError):
    """Raised when an upstream response cannot be decoded."""


class ConcurrentGenerationError(APIError):
    """Raised when the upstream rejects concurrent generation."""


def api_error_status_code(exc: APIError) -> int:
    if isinstance(exc, DataSerializationError):
        return 502
    if not str(exc.code or "").isdigit():
        return 502
    status_code = int(exc.code)
    # 上游可能返回 2xx 但 Content-Type 不在白名单（网关拦截页等），此时错误对象
    # 携带的是 2xx。直接透传会让客户端把失败当成功，也会把 usage_logs.error_code
    # 记成 201。任何非错误码一律折叠为 502。
    if status_code < 400:
        return 502
    return status_code
