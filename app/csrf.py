from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .cookies import delete_response_cookie, set_response_cookie
from .signed_tokens import expiring_payload, sign_payload, verify_payload


ADMIN_SESSION_COOKIE = "novelai_proxy_admin"
SELF_SERVICE_SESSION_COOKIE = "novelai_proxy_self_service_session"
ADMIN_CSRF_COOKIE = "novelai_proxy_admin_csrf"
SELF_SERVICE_CSRF_COOKIE = "novelai_proxy_self_service_csrf"
CSRF_TTL_SECONDS = 8 * 60 * 60
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from .admin.auth import has_valid_admin_basic_authorization

        valid_basic_authorization = (
            request.url.path.startswith("/admin/api")
            and has_valid_admin_basic_authorization(request)
        )
        if (
            valid_basic_authorization
            and request.method.upper() not in SAFE_METHODS
            and _is_cross_site_browser_request(request)
        ):
            return JSONResponse(
                status_code=403,
                content={"message": "不允许跨站使用 Basic 认证的管理请求"},
            )

        legacy_scope = _legacy_session_scope(request, valid_basic_authorization)
        if legacy_scope is not None:
            return _clear_legacy_session(request, legacy_scope)

        scope = _csrf_scope(request, valid_basic_authorization)
        if scope is None:
            return await call_next(request)

        token = _valid_cookie_token(request, scope)
        if request.method.upper() not in SAFE_METHODS:
            submitted = request.headers.get("x-csrf-token")
            if (
                not submitted
                and request.headers.get("content-type", "").split(";", 1)[0] == "application/x-www-form-urlencoded"
            ):
                values = parse_qs((await request.body()).decode("utf-8", errors="replace"))
                submitted = str((values.get("_csrf_token") or [""])[0])
            if token is None or not submitted or not hmac.compare_digest(submitted, token):
                return JSONResponse(
                    status_code=403,
                    content={"message": "CSRF 令牌缺失或无效，请刷新页面后重试"},
                )

        if token is None:
            token = _new_token(request, scope)
        request.state.csrf_token = token
        response = await call_next(request)
        if request.cookies.get(_csrf_cookie_name(scope)) != token:
            set_response_cookie(
                response,
                request,
                _csrf_cookie_name(scope),
                token,
                max_age=CSRF_TTL_SECONDS,
                httponly=False,
            )
        return response


def _legacy_session_scope(
    request: Request,
    valid_basic_authorization: bool,
) -> Literal["admin", "self_service"] | None:
    path = request.url.path
    if (
        path.startswith("/admin")
        and path != "/admin/login"
        and request.cookies.get(ADMIN_SESSION_COOKIE)
        and not valid_basic_authorization
    ):
        from .admin.auth import admin_session_payload

        payload = admin_session_payload(request)
        if payload is not None and not _payload_has_session_id(payload):
            return "admin"
    if path.startswith("/account") and request.cookies.get(SELF_SERVICE_SESSION_COOKIE):
        payload = verify_payload(
            request.cookies.get(SELF_SERVICE_SESSION_COOKIE),
            request.app.state.config.self_service.discord.session_secret,
        )
        if payload is not None and not _payload_has_session_id(payload):
            return "self_service"
    return None


def _clear_legacy_session(request: Request, scope: Literal["admin", "self_service"]) -> Response:
    login_path = "/admin/login" if scope == "admin" else "/signup"
    if request.method.upper() in SAFE_METHODS:
        response: Response = RedirectResponse(login_path, status_code=303)
    else:
        response = JSONResponse(
            status_code=401,
            content={"message": "会话已过期，请重新登录", "login_url": login_path},
        )
    delete_response_cookie(response, request, _session_cookie_name(scope))
    delete_response_cookie(response, request, _csrf_cookie_name(scope))
    return response


def _csrf_scope(
    request: Request,
    valid_basic_authorization: bool,
) -> Literal["admin", "self_service"] | None:
    path = request.url.path
    if (
        path.startswith("/admin")
        and path != "/admin/login"
        and request.cookies.get(ADMIN_SESSION_COOKIE)
        and not valid_basic_authorization
    ):
        from .admin.auth import admin_session_id

        if admin_session_id(request) is not None:
            return "admin"
    if path.startswith("/account") and request.cookies.get(SELF_SERVICE_SESSION_COOKIE):
        payload = verify_payload(
            request.cookies.get(SELF_SERVICE_SESSION_COOKIE),
            request.app.state.config.self_service.discord.session_secret,
        )
        if payload is not None:
            return "self_service"
    return None


def _valid_cookie_token(request: Request, scope: Literal["admin", "self_service"]) -> str | None:
    token = request.cookies.get(_csrf_cookie_name(scope))
    payload = verify_payload(token, _csrf_secret(request, scope))
    session_id = _session_id(request, scope)
    if payload is None or session_id is None:
        return None
    if payload.get("scope") != scope or payload.get("sid") != session_id:
        return None
    return token


def _new_token(request: Request, scope: Literal["admin", "self_service"]) -> str:
    session_id = _session_id(request, scope)
    return sign_payload(
        expiring_payload(CSRF_TTL_SECONDS, scope=scope, sid=session_id, nonce=secrets.token_urlsafe(18)),
        _csrf_secret(request, scope),
    )


def _session_id(request: Request, scope: Literal["admin", "self_service"]) -> str | None:
    if scope == "admin":
        from .admin.auth import admin_session_id

        return admin_session_id(request)
    else:
        payload = verify_payload(
            request.cookies.get(SELF_SERVICE_SESSION_COOKIE),
            request.app.state.config.self_service.discord.session_secret,
        )
    if payload is None:
        return None
    session_id = payload.get("sid")
    return session_id if isinstance(session_id, str) and session_id else None


def _is_cross_site_browser_request(request: Request) -> bool:
    if request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return True

    origin = request.headers.get("origin")
    if origin is not None:
        return not _url_matches_request_origin(request, origin)

    referer = request.headers.get("referer")
    if referer:
        return not _url_matches_request_origin(request, referer)
    return False


def _url_matches_request_origin(request: Request, value: str) -> bool:
    try:
        candidate = urlsplit(value)
        candidate_port = candidate.port
    except ValueError:
        return False
    if not candidate.scheme or candidate.hostname is None:
        return False

    request_port = request.url.port or _default_port(request.url.scheme)
    candidate_port = candidate_port or _default_port(candidate.scheme)
    return (
        candidate.scheme.lower() == request.url.scheme.lower()
        and candidate.hostname.lower() == (request.url.hostname or "").lower()
        and candidate_port == request_port
    )


def _default_port(scheme: str) -> int | None:
    normalized = scheme.lower()
    if normalized == "http":
        return 80
    if normalized == "https":
        return 443
    return None


def _payload_has_session_id(payload: dict) -> bool:
    session_id = payload.get("sid")
    return isinstance(session_id, str) and bool(session_id)


def _csrf_secret(request: Request, scope: Literal["admin", "self_service"]) -> str:
    if scope == "admin":
        from .admin.auth import admin_session_secret

        source = admin_session_secret(request)
    else:
        source = request.app.state.config.self_service.discord.session_secret
    return hashlib.sha256(f"{source}:csrf:v1".encode("utf-8")).hexdigest()


def _csrf_cookie_name(scope: Literal["admin", "self_service"]) -> str:
    return ADMIN_CSRF_COOKIE if scope == "admin" else SELF_SERVICE_CSRF_COOKIE


def _session_cookie_name(scope: Literal["admin", "self_service"]) -> str:
    return ADMIN_SESSION_COOKIE if scope == "admin" else SELF_SERVICE_SESSION_COOKIE
