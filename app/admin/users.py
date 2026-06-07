from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..database import Database, utc_now_iso
from ..users import (
    CreateUserInput,
    UpdateUserInput,
    create_user as create_user_record,
    delete_user as delete_user_record,
    ensure_user_exists,
    reset_api_key,
    update_user as update_user_record,
)
from .auth import has_admin_session, require_admin
from .common import (
    ALLOWED_ENDPOINT_CHOICES,
    DEFAULT_ALLOWED_ENDPOINTS,
    row_to_dict,
    templates,
    user_row_to_dict,
)


api_router = APIRouter(prefix="/admin/api")
web_router = APIRouter(prefix="/admin")
API_KEY_FLASH_COOKIE = "novelai_proxy_api_key_flash"
API_KEY_FLASH_TTL_SECONDS = 5 * 60


class CreateUserRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    tier: str = Field(default="normal", pattern="^(normal|vip)$")
    free_small_only: bool = False
    allowed_endpoints: list[str] = Field(default_factory=lambda: [DEFAULT_ALLOWED_ENDPOINTS])
    allowed_upstreams: list[str] = Field(default_factory=list)
    anlas_total: int = Field(default=0, ge=0)
    reset_period: str = Field(default="month", pattern="^(month|week|day|never)$")
    reset_day: int | None = Field(default=None, ge=0, le=28)


class UpdateUserRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    tier: str | None = Field(default=None, pattern="^(normal|vip)$")
    is_active: bool | None = None
    free_small_only: bool | None = None
    allowed_endpoints: list[str] | None = None
    allowed_upstreams: list[str] | None = None
    anlas_total: int | None = Field(default=None, ge=0)
    reset_period: str | None = Field(default=None, pattern="^(month|week|day|never)$")
    reset_day: int | None = Field(default=None, ge=0, le=28)


class RateLimitRuleRequest(BaseModel):
    period: str = Field(..., pattern="^(minute|hour|day|month)$")
    max_requests: int = Field(..., ge=1)
    is_active: bool = True


@api_router.get("/users", dependencies=[Depends(require_admin)])
async def list_users(request: Request):
    db: Database = request.app.state.db
    rows = db.query_all(
        """
        SELECT u.id, u.name, u.tier, u.is_active, u.free_small_only, u.allowed_endpoints, u.allowed_upstreams, u.created_at,
               NULL AS api_key,
               COALESCE(q.total, 0) AS anlas_total,
               COALESCE(q.used, 0) AS anlas_used,
               COALESCE(q.reserved, 0) AS anlas_reserved
        FROM users u
        LEFT JOIN user_anlas_quota q ON q.user_id = u.id
        WHERE u.deleted_at IS NULL
        ORDER BY u.id DESC
        """
    )
    return {"users": [user_row_to_dict(row) for row in rows]}


@api_router.post("/users", dependencies=[Depends(require_admin)])
async def create_user(payload: CreateUserRequest, request: Request):
    db: Database = request.app.state.db
    _validate_allowed_endpoints(payload.allowed_endpoints)
    _validate_allowed_upstreams(payload.allowed_upstreams, request)
    created = create_user_record(
        db,
        request.app.state.quota_manager,
        CreateUserInput(
            name=payload.name,
            tier=payload.tier,
            free_small_only=payload.free_small_only,
            allowed_endpoints=payload.allowed_endpoints,
            allowed_upstreams=payload.allowed_upstreams,
            anlas_total=payload.anlas_total,
            reset_period=payload.reset_period,
            reset_day=payload.reset_day,
        ),
    )
    _notify_dashboard_change(request)
    return {"user_id": created.user_id, "api_key": created.api_key}


@api_router.patch("/users/{user_id}", dependencies=[Depends(require_admin)])
async def update_user(user_id: int, payload: UpdateUserRequest, request: Request):
    db: Database = request.app.state.db
    ensure_user_exists(db, user_id)
    if payload.allowed_endpoints is not None:
        _validate_allowed_endpoints(payload.allowed_endpoints)
    if payload.allowed_upstreams is not None:
        _validate_allowed_upstreams(payload.allowed_upstreams, request)
    changed = update_user_record(
        db,
        request.app.state.quota_manager,
        user_id,
        UpdateUserInput(
            name=payload.name,
            tier=payload.tier,
            is_active=payload.is_active,
            free_small_only=payload.free_small_only,
            allowed_endpoints=payload.allowed_endpoints,
            allowed_upstreams=payload.allowed_upstreams,
            anlas_total=payload.anlas_total,
            reset_period=payload.reset_period,
            reset_day=payload.reset_day,
        ),
    )
    if changed:
        _notify_dashboard_change(request)
    return {"ok": True}


@api_router.delete("/users/{user_id}", dependencies=[Depends(require_admin)])
async def delete_user(user_id: int, request: Request):
    db: Database = request.app.state.db
    delete_user_record(db, user_id)
    _notify_dashboard_change(request)
    return {"ok": True}


@api_router.post("/users/{user_id}/reset-quota", dependencies=[Depends(require_admin)])
async def reset_user_quota(user_id: int, request: Request):
    ensure_user_exists(request.app.state.db, user_id)
    request.app.state.quota_manager.reset_usage(user_id)
    _notify_dashboard_change(request)
    return {"ok": True}


@api_router.post("/users/{user_id}/reset-key", dependencies=[Depends(require_admin)])
async def reset_user_key(user_id: int, request: Request):
    db: Database = request.app.state.db
    api_key = reset_api_key(db, user_id)
    return {"user_id": user_id, "api_key": api_key}


@api_router.post("/users/{user_id}/rate-limit-rules", dependencies=[Depends(require_admin)])
async def add_rate_limit_rule(user_id: int, payload: RateLimitRuleRequest, request: Request):
    db: Database = request.app.state.db
    ensure_user_exists(db, user_id)
    now = utc_now_iso()
    db.execute(
        """
        INSERT INTO rate_limit_rules (user_id, period, max_requests, is_active, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, payload.period, payload.max_requests, 1 if payload.is_active else 0, now),
    )
    return {"ok": True}


@api_router.patch("/rate-limit-rules/{rule_id}", dependencies=[Depends(require_admin)])
async def update_rate_limit_rule(rule_id: int, payload: RateLimitRuleRequest, request: Request):
    db: Database = request.app.state.db
    _ensure_rate_limit_rule_exists(db, rule_id)
    db.execute(
        """
        UPDATE rate_limit_rules
        SET period = ?, max_requests = ?, is_active = ?
        WHERE id = ?
        """,
        (payload.period, payload.max_requests, 1 if payload.is_active else 0, rule_id),
    )
    return {"ok": True}


@api_router.delete("/rate-limit-rules/{rule_id}", dependencies=[Depends(require_admin)])
async def delete_rate_limit_rule(rule_id: int, request: Request):
    db: Database = request.app.state.db
    _ensure_rate_limit_rule_exists(db, rule_id)
    db.execute("DELETE FROM rate_limit_rules WHERE id = ?", (rule_id,))
    return {"ok": True}


@web_router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    db: Database = request.app.state.db
    new_api_key = _pop_api_key_flash(request)
    rows = db.query_all(
        """
        SELECT u.id, u.name, u.tier, u.is_active, u.free_small_only, u.allowed_endpoints, u.allowed_upstreams, u.created_at,
               NULL AS api_key,
               COALESCE(q.total, 0) AS anlas_total,
               COALESCE(q.used, 0) AS anlas_used,
               COALESCE(q.reserved, 0) AS anlas_reserved,
               COALESCE(q.reset_period, 'month') AS reset_period,
               COALESCE(q.reset_day, 1) AS reset_day
        FROM users u
        LEFT JOIN user_anlas_quota q ON q.user_id = u.id
        WHERE u.deleted_at IS NULL
        ORDER BY u.id DESC
        """
    )
    response = templates.TemplateResponse(
        request,
        "users.html",
        {
            "active": "users",
            "users": [user_row_to_dict(row) for row in rows],
            "endpoint_choices": ALLOWED_ENDPOINT_CHOICES,
            "upstream_choices": _upstream_choices(request),
            "new_api_key": new_api_key,
        },
    )
    if new_api_key is not None:
        response.delete_cookie(API_KEY_FLASH_COOKIE)
    return response


@web_router.post("/users")
async def create_user_form(
    request: Request,
    name: str = Form(...),
    tier: str = Form("normal"),
    anlas_total: int = Form(0),
    reset_period: str = Form("month"),
    reset_day: int | None = Form(None),
    free_small_only: str | None = Form(None),
    allowed_endpoints: list[str] | None = Form(None),
    allowed_upstreams: list[str] | None = Form(None),
):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    result = await create_user(
        CreateUserRequest(
            name=name,
            tier=tier,
            anlas_total=anlas_total,
            reset_period=reset_period,
            reset_day=reset_day,
            free_small_only=free_small_only == "on",
            allowed_endpoints=allowed_endpoints or [],
            allowed_upstreams=allowed_upstreams or [],
        ),
        request,
    )
    response = RedirectResponse("/admin/users", status_code=303)
    _set_api_key_flash(response, request, result["api_key"])
    return response


@web_router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_edit_page(user_id: int, request: Request):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    db: Database = request.app.state.db
    new_api_key = _pop_api_key_flash(request)
    user = db.query_one(
        """
        SELECT u.id, u.name, u.tier, u.is_active, u.free_small_only, u.allowed_endpoints, u.allowed_upstreams, NULL AS api_key,
               q.total AS anlas_total, q.used AS anlas_used, q.reserved AS anlas_reserved,
               q.reset_period, q.reset_day
        FROM users u
        LEFT JOIN user_anlas_quota q ON q.user_id = u.id
        WHERE u.id = ? AND u.deleted_at IS NULL
        """,
        (user_id,),
    )
    if user is None:
        raise HTTPException(status_code=404, detail={"message": "User not found"})
    rules = db.query_all(
        "SELECT id, period, max_requests, is_active FROM rate_limit_rules WHERE user_id = ? ORDER BY id",
        (user_id,),
    )
    response = templates.TemplateResponse(
        request,
        "user_edit.html",
        {
            "active": "users",
            "user": user_row_to_dict(user),
            "rules": [row_to_dict(row) for row in rules],
            "endpoint_choices": ALLOWED_ENDPOINT_CHOICES,
            "upstream_choices": _upstream_choices(request),
            "new_api_key": new_api_key,
        },
    )
    if new_api_key is not None:
        response.delete_cookie(API_KEY_FLASH_COOKIE)
    return response


@web_router.post("/users/{user_id}")
async def update_user_form(
    user_id: int,
    request: Request,
    name: str = Form(...),
    tier: str = Form("normal"),
    is_active: str | None = Form(None),
    anlas_total: int = Form(0),
    reset_period: str = Form("month"),
    reset_day: int = Form(1),
    free_small_only: str | None = Form(None),
    allowed_endpoints: list[str] | None = Form(None),
    allowed_upstreams: list[str] | None = Form(None),
):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await update_user(
        user_id,
        UpdateUserRequest(
            name=name,
            tier=tier,
            is_active=is_active == "on",
            free_small_only=free_small_only == "on",
            anlas_total=anlas_total,
            reset_period=reset_period,
            reset_day=reset_day,
            allowed_endpoints=allowed_endpoints or [],
            allowed_upstreams=allowed_upstreams or [],
        ),
        request,
    )
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@web_router.post("/users/{user_id}/delete")
async def delete_user_form(user_id: int, request: Request):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await delete_user(user_id, request)
    return RedirectResponse("/admin/users", status_code=303)


@web_router.post("/users/{user_id}/reset-quota")
async def reset_quota_form(user_id: int, request: Request):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await reset_user_quota(user_id, request)
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@web_router.post("/users/{user_id}/reset-key")
async def reset_key_form(user_id: int, request: Request):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    result = await reset_user_key(user_id, request)
    response = RedirectResponse(f"/admin/users/{user_id}", status_code=303)
    _set_api_key_flash(response, request, result["api_key"])
    return response


@web_router.post("/users/{user_id}/rate-limit-rules")
async def add_rate_limit_rule_form(
    user_id: int,
    request: Request,
    period: str = Form(...),
    max_requests: int = Form(...),
):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await add_rate_limit_rule(user_id, RateLimitRuleRequest(period=period, max_requests=max_requests), request)
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@web_router.post("/rate-limit-rules/{rule_id}")
async def update_rate_limit_rule_form(
    rule_id: int,
    request: Request,
    user_id: int = Form(...),
    period: str = Form(...),
    max_requests: int = Form(...),
    is_active: str | None = Form(None),
):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await update_rate_limit_rule(
        rule_id,
        RateLimitRuleRequest(period=period, max_requests=max_requests, is_active=is_active == "on"),
        request,
    )
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@web_router.post("/rate-limit-rules/{rule_id}/delete")
async def delete_rate_limit_rule_form(rule_id: int, request: Request, user_id: int = Form(...)):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await delete_rate_limit_rule(rule_id, request)
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


def _ensure_rate_limit_rule_exists(db: Database, rule_id: int) -> None:
    row = db.query_one(
        """
        SELECT r.id
        FROM rate_limit_rules r
        JOIN users u ON u.id = r.user_id
        WHERE r.id = ? AND u.deleted_at IS NULL
        """,
        (rule_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail={"message": "Rate limit rule not found"})


def _validate_allowed_endpoints(allowed_endpoints: list[str] | None) -> None:
    if allowed_endpoints is None:
        return
    normalized = [item.strip() for item in allowed_endpoints if item.strip()]
    if not normalized:
        raise HTTPException(status_code=400, detail={"message": "At least one endpoint must be allowed"})
    unknown = sorted({item for item in normalized if item not in ALLOWED_ENDPOINT_CHOICES})
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"message": f"Unknown endpoint: {', '.join(unknown)}"},
        )


def _set_api_key_flash(response: Response, request: Request, api_key: str) -> None:
    token = secrets.token_urlsafe(24)
    _cleanup_api_key_flash_store(request)
    _api_key_flash_store(request)[token] = (api_key, time.monotonic() + API_KEY_FLASH_TTL_SECONDS)
    response.set_cookie(
        API_KEY_FLASH_COOKIE,
        token,
        max_age=API_KEY_FLASH_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )


def _pop_api_key_flash(request: Request) -> str | None:
    token = request.cookies.get(API_KEY_FLASH_COOKIE)
    if not token:
        return None
    _cleanup_api_key_flash_store(request)
    value = _api_key_flash_store(request).pop(token, None)
    if value is None:
        return None
    api_key, expires_at = value
    if time.monotonic() > expires_at:
        return None
    return api_key


def _api_key_flash_store(request: Request) -> dict[str, tuple[str, float]]:
    store = getattr(request.app.state, "admin_api_key_flash_store", None)
    if store is None:
        store = {}
        request.app.state.admin_api_key_flash_store = store
    return store


def _cleanup_api_key_flash_store(request: Request) -> None:
    now = time.monotonic()
    store = _api_key_flash_store(request)
    for token, (_, expires_at) in list(store.items()):
        if expires_at <= now:
            store.pop(token, None)


def _upstream_choices(request: Request) -> list[str]:
    clients = getattr(request.app.state, "upstream_clients", None)
    if isinstance(clients, dict) and clients:
        return list(clients.keys())
    return ["default"]


def _validate_allowed_upstreams(allowed_upstreams: list[str] | None, request: Request) -> None:
    if not allowed_upstreams:
        return
    valid_upstreams = set(_upstream_choices(request))
    unknown = sorted({item.strip() for item in allowed_upstreams if item.strip()} - valid_upstreams)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"message": f"Unknown upstream id: {', '.join(unknown)}"},
        )


def _notify_dashboard_change(request: Request) -> None:
    event_bus = getattr(request.app.state, "dashboard_events", None)
    if event_bus is not None:
        event_bus.notify_nowait()
