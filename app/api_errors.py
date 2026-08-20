from __future__ import annotations

from typing import Any


class NovelAIProxyError(Exception):
    """Base exception for errors returned by the NovelAI upstream."""

    message: str

    def __init__(self, message: str) -> None:
        # Keep the SDK's BaseException.args behavior for compatibility.  The
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
    return int(exc.code) if str(exc.code or "").isdigit() else 502
