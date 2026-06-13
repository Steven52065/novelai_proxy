from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .auth import set_admin_session_cookie, valid_admin_session


class AdminSessionRefreshMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if _should_refresh_admin_session(request, response):
            set_admin_session_cookie(response, request)
        return response


def _should_refresh_admin_session(request: Request, response: Response) -> bool:
    return (
        request.url.path.startswith("/admin")
        and request.url.path != "/admin/login"
        and request.url.path != "/admin/logout"
        and valid_admin_session(request)
        and response.status_code < 400
    )
