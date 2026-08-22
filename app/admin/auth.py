from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from ..security import constant_time_equal
from ..cookies import delete_response_cookie, set_response_cookie
from ..signed_tokens import expiring_payload, sign_payload, verify_payload
from ..templating import templates


web_router = APIRouter(prefix="/admin")
security = HTTPBasic()
optional_security = HTTPBasic(auto_error=False)
SESSION_COOKIE = "novelai_proxy_admin"
SESSION_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def require_admin(request: Request, credentials: HTTPBasicCredentials = Depends(security)) -> None:
    if not _admin_credentials_are_valid(request, credentials.username, credentials.password):
        raise HTTPException(status_code=401, detail={"message": "管理员凭据无效"})


def require_admin_or_session(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(optional_security),
) -> None:
    if has_admin_session(request):
        return
    if credentials is None:
        raise HTTPException(status_code=401, detail={"message": "管理员凭据无效"})
    require_admin(request, credentials)


def has_valid_admin_basic_authorization(request: Request) -> bool:
    scheme, separator, encoded = request.headers.get("authorization", "").partition(" ")
    if not separator or scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError):
        return False
    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    return _admin_credentials_are_valid(request, username, password)


def _admin_credentials_are_valid(request: Request, username: str, password: str) -> bool:
    config = request.app.state.config
    valid_user = constant_time_equal(username, config.admin.username)
    valid_password = constant_time_equal(password, config.admin.password)
    return valid_user and valid_password


class AdminLoginRequired(Exception):
    """管理后台页面缺少有效会话时由依赖抛出，应用层 handler 统一重定向到登录页。"""


def require_admin_page_session(request: Request) -> None:
    if not has_admin_session(request):
        raise AdminLoginRequired()


async def admin_login_required_handler(request: Request, exc: AdminLoginRequired) -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


@web_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@web_router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    config = request.app.state.config
    if not (constant_time_equal(username, config.admin.username) and constant_time_equal(password, config.admin.password)):
        return templates.TemplateResponse(request, "login.html", {"error": "用户名或密码错误"}, status_code=401)
    response = RedirectResponse("/admin", status_code=303)
    set_admin_session_cookie(response, request)
    return response


@web_router.post("/logout")
async def logout(request: Request):
    response = RedirectResponse("/admin/login", status_code=303)
    delete_response_cookie(response, request, SESSION_COOKIE)
    return response


def session_value(request: Request) -> str:
    config = request.app.state.config
    session_id = admin_session_id(request) or secrets.token_urlsafe(18)
    return sign_payload(
        expiring_payload(SESSION_COOKIE_MAX_AGE_SECONDS, sub=config.admin.username, sid=session_id),
        admin_session_secret(request),
    )


def set_admin_session_cookie(response: Response, request: Request) -> None:
    set_response_cookie(
        response,
        request,
        SESSION_COOKIE,
        session_value(request),
        max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
    )


def has_admin_session(request: Request) -> bool:
    return admin_session_id(request) is not None


def admin_session_id(request: Request) -> str | None:
    payload = admin_session_payload(request)
    if payload is None:
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not hmac.compare_digest(sub, request.app.state.config.admin.username):
        return None
    session_id = payload.get("sid")
    return session_id if isinstance(session_id, str) and session_id else None


def admin_session_payload(request: Request) -> dict | None:
    return verify_payload(request.cookies.get(SESSION_COOKIE), admin_session_secret(request))


def admin_session_secret(request: Request) -> str:
    # Admin sessions are intentionally derived from the current admin password:
    # changing that password revokes every outstanding admin cookie.
    password = request.app.state.config.admin.password.encode("utf-8")
    return hmac.new(password, b"novelai-proxy-admin-session-v1", hashlib.sha256).hexdigest()
