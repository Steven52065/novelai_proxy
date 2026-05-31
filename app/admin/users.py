from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..database import Database, utc_now_iso
from ..quota_manager import QuotaManager
from ..security import generate_api_key, hash_api_key
from .auth import has_admin_session, require_admin
from .common import (
    ALLOWED_ENDPOINT_CHOICES,
    DEFAULT_ALLOWED_ENDPOINTS,
    row_to_dict,
    serialize_allowed_endpoints,
    templates,
    user_row_to_dict,
)


api_router = APIRouter(prefix="/admin/api")
web_router = APIRouter(prefix="/admin")


class CreateUserRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    tier: str = Field(default="normal", pattern="^(normal|vip)$")
    free_small_only: bool = False
    allowed_endpoints: list[str] = Field(default_factory=lambda: [DEFAULT_ALLOWED_ENDPOINTS])
    anlas_total: int = Field(default=0, ge=0)
    reset_period: str = Field(default="month", pattern="^(month|week|day|never)$")
    reset_day: int | None = Field(default=None, ge=0, le=28)


class UpdateUserRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    tier: str | None = Field(default=None, pattern="^(normal|vip)$")
    is_active: bool | None = None
    free_small_only: bool | None = None
    allowed_endpoints: list[str] | None = None
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
        SELECT u.id, u.name, u.tier, u.is_active, u.free_small_only, u.allowed_endpoints, u.created_at,
               u.api_key,
               COALESCE(q.total, 0) AS anlas_total,
               COALESCE(q.used, 0) AS anlas_used,
               COALESCE(q.reserved, 0) AS anlas_reserved
        FROM users u
        LEFT JOIN user_anlas_quota q ON q.user_id = u.id
        ORDER BY u.id DESC
        """
    )
    return {"users": [user_row_to_dict(row) for row in rows]}


@api_router.post("/users", dependencies=[Depends(require_admin)])
async def create_user(payload: CreateUserRequest, request: Request):
    db: Database = request.app.state.db
    quota_manager: QuotaManager = request.app.state.quota_manager
    api_key = generate_api_key()
    now = utc_now_iso()
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO users (api_key_hash, api_key, name, tier, is_active, free_small_only, allowed_endpoints, created_at)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                hash_api_key(api_key),
                api_key,
                payload.name,
                payload.tier,
                1 if payload.free_small_only else 0,
                serialize_allowed_endpoints(payload.allowed_endpoints),
                now,
            ),
        )
        user_id = int(cursor.lastrowid)
    quota_manager.create_or_update(user_id, payload.anlas_total, payload.reset_period, payload.reset_day)
    return {"user_id": user_id, "api_key": api_key}


@api_router.patch("/users/{user_id}", dependencies=[Depends(require_admin)])
async def update_user(user_id: int, payload: UpdateUserRequest, request: Request):
    db: Database = request.app.state.db
    fields = []
    params = []
    if payload.name is not None:
        fields.append("name = ?")
        params.append(payload.name)
    if payload.tier is not None:
        fields.append("tier = ?")
        params.append(payload.tier)
    if payload.is_active is not None:
        fields.append("is_active = ?")
        params.append(1 if payload.is_active else 0)
    if payload.free_small_only is not None:
        fields.append("free_small_only = ?")
        params.append(1 if payload.free_small_only else 0)
    if payload.allowed_endpoints is not None:
        fields.append("allowed_endpoints = ?")
        params.append(serialize_allowed_endpoints(payload.allowed_endpoints))
    if fields:
        params.append(user_id)
        db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", tuple(params))

    quota_fields = []
    quota_params = []
    if payload.anlas_total is not None:
        quota_fields.append("total = ?")
        quota_params.append(payload.anlas_total)
    if payload.reset_period is not None:
        quota_fields.append("reset_period = ?")
        quota_params.append(payload.reset_period)
    if payload.reset_day is not None:
        quota_fields.append("reset_day = ?")
        quota_params.append(payload.reset_day)
    if quota_fields:
        quota_params.append(user_id)
        db.execute(f"UPDATE user_anlas_quota SET {', '.join(quota_fields)} WHERE user_id = ?", tuple(quota_params))
    return {"ok": True}


@api_router.delete("/users/{user_id}", dependencies=[Depends(require_admin)])
async def delete_user(user_id: int, request: Request):
    db: Database = request.app.state.db
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"ok": True}


@api_router.post("/users/{user_id}/reset-quota", dependencies=[Depends(require_admin)])
async def reset_user_quota(user_id: int, request: Request):
    request.app.state.quota_manager.reset_usage(user_id)
    return {"ok": True}


@api_router.post("/users/{user_id}/reset-key", dependencies=[Depends(require_admin)])
async def reset_user_key(user_id: int, request: Request):
    db: Database = request.app.state.db
    if db.query_one("SELECT id FROM users WHERE id = ?", (user_id,)) is None:
        raise HTTPException(status_code=404, detail={"message": "User not found"})
    api_key = generate_api_key()
    db.execute(
        "UPDATE users SET api_key_hash = ?, api_key = ? WHERE id = ?",
        (hash_api_key(api_key), api_key, user_id),
    )
    return {"user_id": user_id, "api_key": api_key}


@api_router.post("/users/{user_id}/rate-limit-rules", dependencies=[Depends(require_admin)])
async def add_rate_limit_rule(user_id: int, payload: RateLimitRuleRequest, request: Request):
    db: Database = request.app.state.db
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
    db.execute("DELETE FROM rate_limit_rules WHERE id = ?", (rule_id,))
    return {"ok": True}


@web_router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    db: Database = request.app.state.db
    rows = db.query_all(
        """
        SELECT u.id, u.name, u.tier, u.is_active, u.free_small_only, u.allowed_endpoints, u.created_at,
               u.api_key,
               COALESCE(q.total, 0) AS anlas_total,
               COALESCE(q.used, 0) AS anlas_used,
               COALESCE(q.reserved, 0) AS anlas_reserved,
               COALESCE(q.reset_period, 'month') AS reset_period,
               COALESCE(q.reset_day, 1) AS reset_day
        FROM users u
        LEFT JOIN user_anlas_quota q ON q.user_id = u.id
        ORDER BY u.id DESC
        """
    )
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "active": "users",
            "users": [user_row_to_dict(row) for row in rows],
            "endpoint_choices": ALLOWED_ENDPOINT_CHOICES,
        },
    )


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
            allowed_endpoints=allowed_endpoints or [DEFAULT_ALLOWED_ENDPOINTS],
        ),
        request,
    )
    return RedirectResponse(f"/admin/users?api_key={result['api_key']}", status_code=303)


@web_router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_edit_page(user_id: int, request: Request):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    db: Database = request.app.state.db
    user = db.query_one(
        """
        SELECT u.id, u.name, u.tier, u.is_active, u.free_small_only, u.allowed_endpoints, u.api_key,
               q.total AS anlas_total, q.used AS anlas_used, q.reserved AS anlas_reserved,
               q.reset_period, q.reset_day
        FROM users u
        LEFT JOIN user_anlas_quota q ON q.user_id = u.id
        WHERE u.id = ?
        """,
        (user_id,),
    )
    if user is None:
        raise HTTPException(status_code=404, detail={"message": "User not found"})
    rules = db.query_all(
        "SELECT id, period, max_requests, is_active FROM rate_limit_rules WHERE user_id = ? ORDER BY id",
        (user_id,),
    )
    return templates.TemplateResponse(
        request,
        "user_edit.html",
        {
            "active": "users",
            "user": user_row_to_dict(user),
            "rules": [row_to_dict(row) for row in rules],
            "endpoint_choices": ALLOWED_ENDPOINT_CHOICES,
        },
    )


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
            allowed_endpoints=allowed_endpoints or [DEFAULT_ALLOWED_ENDPOINTS],
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
    return RedirectResponse(f"/admin/users/{user_id}?api_key={result['api_key']}", status_code=303)


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
