from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..database import Database, utc_now_iso
from ..users import (
    UserGroupInput,
    UserGroupUpdateInput,
    create_group,
    delete_or_disable_group,
    get_group,
    list_groups,
    sync_group_members,
    update_group,
)
from ..users.service import parse_allowed_endpoints, parse_allowed_upstreams
from .auth import has_admin_session, require_admin
from .common import ALLOWED_ENDPOINT_CHOICES, DEFAULT_ALLOWED_ENDPOINTS, row_to_dict, templates


api_router = APIRouter(prefix="/admin/api")
web_router = APIRouter(prefix="/admin")


class CreateUserGroupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    is_active: bool = True
    default_tier: str = Field(default="normal", pattern="^(normal|vip)$")
    default_free_small_only: bool = True
    default_allowed_endpoints: list[str] = Field(default_factory=lambda: [DEFAULT_ALLOWED_ENDPOINTS])
    default_allowed_upstreams: list[str] = Field(default_factory=list)
    default_anlas_total: int = Field(default=0, ge=0)
    default_reset_period: str = Field(default="month", pattern="^(month|week|day|never)$")
    default_reset_day: int = Field(default=1, ge=0, le=28)


class UpdateUserGroupRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None
    default_tier: str | None = Field(default=None, pattern="^(normal|vip)$")
    default_free_small_only: bool | None = None
    default_allowed_endpoints: list[str] | None = None
    default_allowed_upstreams: list[str] | None = None
    default_anlas_total: int | None = Field(default=None, ge=0)
    default_reset_period: str | None = Field(default=None, pattern="^(month|week|day|never)$")
    default_reset_day: int | None = Field(default=None, ge=0, le=28)


class SyncGroupMembersRequest(BaseModel):
    fields: list[Literal["tier", "free_small_only", "allowed_endpoints", "allowed_upstreams", "anlas_quota"]] = Field(
        default_factory=list
    )


class GroupRateLimitRuleRequest(BaseModel):
    period: str = Field(..., pattern="^(minute|hour|day|month)$")
    max_requests: int = Field(..., ge=1)
    is_active: bool = True


@api_router.get("/user-groups", dependencies=[Depends(require_admin)])
async def list_user_groups(request: Request):
    db: Database = request.app.state.db
    return {"groups": [_group_row_to_dict(row) for row in list_groups(db)]}


@api_router.post("/user-groups", dependencies=[Depends(require_admin)])
async def create_user_group(payload: CreateUserGroupRequest, request: Request):
    db: Database = request.app.state.db
    _validate_allowed_endpoints(payload.default_allowed_endpoints)
    _validate_allowed_upstreams(payload.default_allowed_upstreams, request)
    group_id = create_group(
        db,
        UserGroupInput(
            name=payload.name,
            is_active=payload.is_active,
            default_tier=payload.default_tier,
            default_free_small_only=payload.default_free_small_only,
            default_allowed_endpoints=payload.default_allowed_endpoints,
            default_allowed_upstreams=payload.default_allowed_upstreams,
            default_anlas_total=payload.default_anlas_total,
            default_reset_period=payload.default_reset_period,
            default_reset_day=payload.default_reset_day,
        ),
    )
    _notify_dashboard_change(request)
    return {"group_id": group_id, "group": _group_row_to_dict(get_group(db, group_id))}


@api_router.get("/user-groups/{group_id}", dependencies=[Depends(require_admin)])
async def get_user_group(group_id: int, request: Request):
    db: Database = request.app.state.db
    return {"group": _group_row_to_dict(get_group(db, group_id))}


@api_router.patch("/user-groups/{group_id}", dependencies=[Depends(require_admin)])
async def patch_user_group(group_id: int, payload: UpdateUserGroupRequest, request: Request):
    db: Database = request.app.state.db
    if payload.default_allowed_endpoints is not None:
        _validate_allowed_endpoints(payload.default_allowed_endpoints)
    if payload.default_allowed_upstreams is not None:
        _validate_allowed_upstreams(payload.default_allowed_upstreams, request)
    changed = update_group(
        db,
        group_id,
        UserGroupUpdateInput(
            name=payload.name,
            is_active=payload.is_active,
            default_tier=payload.default_tier,
            default_free_small_only=payload.default_free_small_only,
            default_allowed_endpoints=payload.default_allowed_endpoints,
            default_allowed_upstreams=payload.default_allowed_upstreams,
            default_anlas_total=payload.default_anlas_total,
            default_reset_period=payload.default_reset_period,
            default_reset_day=payload.default_reset_day,
        ),
    )
    if changed:
        _notify_dashboard_change(request)
    return {"ok": True}


@api_router.delete("/user-groups/{group_id}", dependencies=[Depends(require_admin)])
async def delete_user_group(group_id: int, request: Request):
    db: Database = request.app.state.db
    delete_or_disable_group(db, group_id)
    _notify_dashboard_change(request)
    return {"ok": True}


@api_router.post("/user-groups/{group_id}/sync-members", dependencies=[Depends(require_admin)])
async def sync_user_group_members(group_id: int, payload: SyncGroupMembersRequest, request: Request):
    updated_users = sync_group_members(
        request.app.state.db,
        request.app.state.quota_manager,
        group_id,
        payload.fields,
    )
    if updated_users:
        _notify_dashboard_change(request)
    return {"ok": True, "updated_users": updated_users}


@api_router.post("/user-groups/{group_id}/rate-limit-rules", dependencies=[Depends(require_admin)])
async def add_group_rate_limit_rule(group_id: int, payload: GroupRateLimitRuleRequest, request: Request):
    db: Database = request.app.state.db
    get_group(db, group_id)
    db.execute(
        """
        INSERT INTO group_rate_limit_rules (group_id, period, max_requests, is_active, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (group_id, payload.period, payload.max_requests, 1 if payload.is_active else 0, utc_now_iso()),
    )
    return {"ok": True}


@api_router.patch("/group-rate-limit-rules/{rule_id}", dependencies=[Depends(require_admin)])
async def update_group_rate_limit_rule(rule_id: int, payload: GroupRateLimitRuleRequest, request: Request):
    db: Database = request.app.state.db
    _ensure_group_rate_limit_rule_exists(db, rule_id)
    db.execute(
        """
        UPDATE group_rate_limit_rules
        SET period = ?, max_requests = ?, is_active = ?
        WHERE id = ?
        """,
        (payload.period, payload.max_requests, 1 if payload.is_active else 0, rule_id),
    )
    return {"ok": True}


@api_router.delete("/group-rate-limit-rules/{rule_id}", dependencies=[Depends(require_admin)])
async def delete_group_rate_limit_rule(rule_id: int, request: Request):
    db: Database = request.app.state.db
    _ensure_group_rate_limit_rule_exists(db, rule_id)
    db.execute("DELETE FROM group_rate_limit_rules WHERE id = ?", (rule_id,))
    return {"ok": True}


@web_router.get("/user-groups", response_class=HTMLResponse)
async def user_groups_page(request: Request):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    db: Database = request.app.state.db
    return templates.TemplateResponse(
        request,
        "user_groups.html",
        {
            "active": "user_groups",
            "groups": [_group_row_to_dict(row) for row in list_groups(db)],
            "endpoint_choices": ALLOWED_ENDPOINT_CHOICES,
            "upstream_choices": _upstream_choices(request),
        },
    )


@web_router.post("/user-groups")
async def create_user_group_form(
    request: Request,
    name: str = Form(...),
    is_active: str | None = Form(None),
    default_tier: str = Form("normal"),
    default_free_small_only: str | None = Form(None),
    default_allowed_endpoints: list[str] | None = Form(None),
    default_allowed_upstreams: list[str] | None = Form(None),
    default_anlas_total: int = Form(0),
    default_reset_period: str = Form("month"),
    default_reset_day: int = Form(1),
):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await create_user_group(
        CreateUserGroupRequest(
            name=name,
            is_active=is_active == "on",
            default_tier=default_tier,
            default_free_small_only=default_free_small_only == "on",
            default_allowed_endpoints=default_allowed_endpoints or [],
            default_allowed_upstreams=default_allowed_upstreams or [],
            default_anlas_total=default_anlas_total,
            default_reset_period=default_reset_period,
            default_reset_day=default_reset_day,
        ),
        request,
    )
    return RedirectResponse("/admin/user-groups", status_code=303)


@web_router.get("/user-groups/{group_id}", response_class=HTMLResponse)
async def user_group_edit_page(group_id: int, request: Request):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    db: Database = request.app.state.db
    group = _group_row_to_dict(get_group(db, group_id))
    group_rules = [
        row_to_dict(row)
        for row in db.query_all(
            "SELECT id, period, max_requests, is_active FROM group_rate_limit_rules WHERE group_id = ? ORDER BY id",
            (group_id,),
        )
    ]
    members = [
        row_to_dict(row)
        for row in db.query_all(
            """
            SELECT u.id, u.name, u.tier, u.is_active,
                   COALESCE(q.total, 0) AS anlas_total,
                   COALESCE(q.used, 0) AS anlas_used,
                   COALESCE(q.reserved, 0) AS anlas_reserved
            FROM users u
            LEFT JOIN user_anlas_quota q ON q.user_id = u.id
            WHERE u.group_id = ? AND u.deleted_at IS NULL
            ORDER BY u.id DESC
            """,
            (group_id,),
        )
    ]
    return templates.TemplateResponse(
        request,
        "user_group_edit.html",
        {
            "active": "user_groups",
            "group": group,
            "group_rules": group_rules,
            "members": members,
            "endpoint_choices": ALLOWED_ENDPOINT_CHOICES,
            "upstream_choices": _upstream_choices(request),
        },
    )


@web_router.post("/user-groups/{group_id}")
async def update_user_group_form(
    group_id: int,
    request: Request,
    name: str = Form(...),
    is_active: str | None = Form(None),
    default_tier: str = Form("normal"),
    default_free_small_only: str | None = Form(None),
    default_allowed_endpoints: list[str] | None = Form(None),
    default_allowed_upstreams: list[str] | None = Form(None),
    default_anlas_total: int = Form(0),
    default_reset_period: str = Form("month"),
    default_reset_day: int = Form(1),
):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await patch_user_group(
        group_id,
        UpdateUserGroupRequest(
            name=name,
            is_active=is_active == "on",
            default_tier=default_tier,
            default_free_small_only=default_free_small_only == "on",
            default_allowed_endpoints=default_allowed_endpoints or [],
            default_allowed_upstreams=default_allowed_upstreams or [],
            default_anlas_total=default_anlas_total,
            default_reset_period=default_reset_period,
            default_reset_day=default_reset_day,
        ),
        request,
    )
    return RedirectResponse(f"/admin/user-groups/{group_id}", status_code=303)


@web_router.post("/user-groups/{group_id}/delete")
async def delete_user_group_form(group_id: int, request: Request):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await delete_user_group(group_id, request)
    return RedirectResponse("/admin/user-groups", status_code=303)


@web_router.post("/user-groups/{group_id}/sync-members")
async def sync_user_group_members_form(
    group_id: int,
    request: Request,
    fields: list[str] | None = Form(None),
):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await sync_user_group_members(group_id, SyncGroupMembersRequest(fields=fields or []), request)
    return RedirectResponse(f"/admin/user-groups/{group_id}", status_code=303)


@web_router.post("/user-groups/{group_id}/rate-limit-rules")
async def add_group_rate_limit_rule_form(
    group_id: int,
    request: Request,
    period: str = Form(...),
    max_requests: int = Form(...),
):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await add_group_rate_limit_rule(group_id, GroupRateLimitRuleRequest(period=period, max_requests=max_requests), request)
    return RedirectResponse(f"/admin/user-groups/{group_id}", status_code=303)


@web_router.post("/group-rate-limit-rules/{rule_id}")
async def update_group_rate_limit_rule_form(
    rule_id: int,
    request: Request,
    group_id: int = Form(...),
    period: str = Form(...),
    max_requests: int = Form(...),
    is_active: str | None = Form(None),
):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await update_group_rate_limit_rule(
        rule_id,
        GroupRateLimitRuleRequest(period=period, max_requests=max_requests, is_active=is_active == "on"),
        request,
    )
    return RedirectResponse(f"/admin/user-groups/{group_id}", status_code=303)


@web_router.post("/group-rate-limit-rules/{rule_id}/delete")
async def delete_group_rate_limit_rule_form(rule_id: int, request: Request, group_id: int = Form(...)):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await delete_group_rate_limit_rule(rule_id, request)
    return RedirectResponse(f"/admin/user-groups/{group_id}", status_code=303)


def _group_row_to_dict(row):
    data = row_to_dict(row)
    data["default_allowed_endpoints_list"] = parse_allowed_endpoints(data.get("default_allowed_endpoints"))
    data["default_allowed_upstreams_list"] = parse_allowed_upstreams(data.get("default_allowed_upstreams"))
    return data


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


def _ensure_group_rate_limit_rule_exists(db: Database, rule_id: int) -> None:
    row = db.query_one(
        """
        SELECT r.id
        FROM group_rate_limit_rules r
        JOIN user_groups g ON g.id = r.group_id
        WHERE r.id = ?
        """,
        (rule_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail={"message": "Group rate limit rule not found"})
