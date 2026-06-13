from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from ..security import constant_time_equal
from ..templating import templates


web_router = APIRouter(prefix="/admin")
security = HTTPBasic()
optional_security = HTTPBasic(auto_error=False)
SESSION_COOKIE = "novelai_proxy_admin"
SESSION_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def require_admin(request: Request, credentials: HTTPBasicCredentials = Depends(security)) -> None:
    config = request.app.state.config
    valid_user = constant_time_equal(credentials.username, config.admin.username)
    valid_password = constant_time_equal(credentials.password, config.admin.password)
    if not (valid_user and valid_password):
        raise HTTPException(status_code=401, detail={"message": "Invalid admin credentials"})


def require_admin_or_session(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(optional_security),
) -> None:
    if has_admin_session(request):
        return
    if credentials is None:
        raise HTTPException(status_code=401, detail={"message": "Invalid admin credentials"})
    require_admin(request, credentials)


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
async def logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


def session_value(request: Request) -> str:
    config = request.app.state.config
    payload = config.admin.username
    signature = hmac.digest(config.admin.password.encode(), payload.encode(), "sha256").hex()
    return f"{payload}:{signature}"


def set_admin_session_cookie(response: Response, request: Request) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_value(request),
        max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )


def valid_admin_session(request: Request) -> bool:
    return has_admin_session(request)


def has_admin_session(request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    return hmac.compare_digest(cookie, session_value(request))
