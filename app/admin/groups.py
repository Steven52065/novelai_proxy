from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..allowlists import (
    ALLOWED_ENDPOINT_CHOICES,
    DEFAULT_ALLOWED_ENDPOINTS,
    AllowedEndpoints,
    AllowedUpstreams,
)
from ..database import Database, utc_now_iso
from ..queue_tiers import TIER_NORMAL, USER_TIER_PATTERN
from ..templating import templates
from ..users import (
    UserGroupInput,
    UserGroupUpdateInput,
    create_group,
    delete_or_disable_group,
    get_group,
    list_groups,
    preview_group_propagation,
    sync_group_members,
    update_group_with_propagation,
)
from .auth import require_admin, require_admin_or_session, require_admin_page_session
from .common import (
    normalize_reset_day_or_400,
    notify_dashboard_change,
    row_to_dict,
    upstream_choices,
    validate_allowed_endpoints,
    validate_allowed_upstreams,
    validate_free_small_daily_limit,
)


api_router = APIRouter(prefix="/admin/api")
web_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_page_session)])


class CreateUserGroupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    is_active: bool = True
    default_tier: str = Field(default=TIER_NORMAL, pattern=USER_TIER_PATTERN)
    default_free_small_only: bool = True
    free_small_daily_limit_enabled: bool = False
    free_small_daily_limit: int = Field(default=0, ge=0)
    default_allowed_endpoints: list[str] = Field(default_factory=lambda: [DEFAULT_ALLOWED_ENDPOINTS])
    default_allowed_upstreams: list[str] = Field(default_factory=list)
    default_anlas_total: int = Field(default=0, ge=0)
    default_reset_period: str = Field(default="month", pattern="^(month|week|day|never)$")
    default_reset_day: int = Field(default=1, ge=0, le=28)


class UpdateUserGroupRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None
    default_tier: str | None = Field(default=None, pattern=USER_TIER_PATTERN)
    default_free_small_only: bool | None = None
    free_small_daily_limit_enabled: bool | None = None
    free_small_daily_limit: int | None = Field(default=None, ge=0)
    default_allowed_endpoints: list[str] | None = None
    default_allowed_upstreams: list[str] | None = None
    default_anlas_total: int | None = Field(default=None, ge=0)
    default_reset_period: str | None = Field(default=None, pattern="^(month|week|day|never)$")
    default_reset_day: int | None = Field(default=None, ge=0, le=28)
    propagate: Literal["unmodified", "all", "none"] = "unmodified"


class SyncGroupMembersRequest(BaseModel):
    fields: list[
        Literal[
            "tier",
            "free_small_only",
            "free_small_daily_limit",
            "allowed_endpoints",
            "allowed_upstreams",
            "anlas_quota",
        ]
    ] = Field(default_factory=list)


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
    validate_free_small_daily_limit(payload.free_small_daily_limit_enabled, payload.free_small_daily_limit)
    validate_allowed_endpoints(payload.default_allowed_endpoints)
    validate_allowed_upstreams(payload.default_allowed_upstreams, request)
    default_reset_day = normalize_reset_day_or_400(payload.default_reset_period, payload.default_reset_day)
    group_id = create_group(
        db,
        UserGroupInput(
            name=payload.name,
            is_active=payload.is_active,
            default_tier=payload.default_tier,
            default_free_small_only=payload.default_free_small_only,
            free_small_daily_limit_enabled=payload.free_small_daily_limit_enabled,
            free_small_daily_limit=payload.free_small_daily_limit,
            default_allowed_endpoints=payload.default_allowed_endpoints,
            default_allowed_upstreams=payload.default_allowed_upstreams,
            default_anlas_total=payload.default_anlas_total,
            default_reset_period=payload.default_reset_period,
            default_reset_day=default_reset_day,
        ),
    )
    notify_dashboard_change(request)
    return {"group_id": group_id, "group": _group_row_to_dict(get_group(db, group_id))}


@api_router.get("/user-groups/{group_id}", dependencies=[Depends(require_admin)])
async def get_user_group(group_id: int, request: Request):
    db: Database = request.app.state.db
    return {"group": _group_row_to_dict(get_group(db, group_id))}


@api_router.patch("/user-groups/{group_id}", dependencies=[Depends(require_admin)])
async def patch_user_group(group_id: int, payload: UpdateUserGroupRequest, request: Request):
    db: Database = request.app.state.db
    update_input = _build_group_update_input(db, group_id, payload, request)
    summary = update_group_with_propagation(
        db,
        request.app.state.quota_manager,
        group_id,
        update_input,
        propagate_scope=payload.propagate,
    )
    if summary["group_changed"] or summary["updated_users"]:
        notify_dashboard_change(request)
    return {"ok": True, "propagation": summary}


@api_router.post("/user-groups/{group_id}/propagation-preview", dependencies=[Depends(require_admin_or_session)])
async def preview_user_group_propagation(group_id: int, payload: UpdateUserGroupRequest, request: Request):
    db: Database = request.app.state.db
    update_input = _build_group_update_input(db, group_id, payload, request)
    return preview_group_propagation(db, group_id, update_input)


def _build_group_update_input(
    db: Database,
    group_id: int,
    payload: UpdateUserGroupRequest,
    request: Request,
) -> UserGroupUpdateInput:
    _validate_update_free_small_daily_limit(db, group_id, payload)
    if payload.default_allowed_endpoints is not None:
        validate_allowed_endpoints(payload.default_allowed_endpoints)
    if payload.default_allowed_upstreams is not None:
        validate_allowed_upstreams(payload.default_allowed_upstreams, request)
    default_reset_day = _normalize_update_reset_day_or_400(db, group_id, payload)
    return UserGroupUpdateInput(
        name=payload.name,
        is_active=payload.is_active,
        default_tier=payload.default_tier,
        default_free_small_only=payload.default_free_small_only,
        free_small_daily_limit_enabled=payload.free_small_daily_limit_enabled,
        free_small_daily_limit=payload.free_small_daily_limit,
        default_allowed_endpoints=payload.default_allowed_endpoints,
        default_allowed_upstreams=payload.default_allowed_upstreams,
        default_anlas_total=payload.default_anlas_total,
        default_reset_period=payload.default_reset_period,
        default_reset_day=default_reset_day,
    )


@api_router.delete("/user-groups/{group_id}", dependencies=[Depends(require_admin)])
async def delete_user_group(group_id: int, request: Request):
    db: Database = request.app.state.db
    delete_or_disable_group(db, group_id)
    notify_dashboard_change(request)
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
        notify_dashboard_change(request)
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
    db: Database = request.app.state.db
    return templates.TemplateResponse(
        request,
        "user_groups.html",
        {
            "active": "user_groups",
            "groups": [_group_row_to_dict(row) for row in list_groups(db)],
            "endpoint_choices": ALLOWED_ENDPOINT_CHOICES,
            "upstream_choices": upstream_choices(request),
        },
    )


@web_router.post("/user-groups")
async def create_user_group_form(
    request: Request,
    name: str = Form(...),
    is_active: str | None = Form(None),
    default_tier: str = Form(TIER_NORMAL),
    default_free_small_only: str | None = Form(None),
    free_small_daily_limit_enabled: str | None = Form(None),
    free_small_daily_limit: int = Form(0),
    default_allowed_endpoints: list[str] | None = Form(None),
    default_allowed_upstreams: list[str] | None = Form(None),
    default_anlas_total: int = Form(0),
    default_reset_period: str = Form("month"),
    default_reset_day: int = Form(1),
):
    await create_user_group(
        CreateUserGroupRequest(
            name=name,
            is_active=is_active == "on",
            default_tier=default_tier,
            default_free_small_only=default_free_small_only == "on",
            free_small_daily_limit_enabled=free_small_daily_limit_enabled == "on",
            free_small_daily_limit=free_small_daily_limit,
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
            "upstream_choices": upstream_choices(request),
            "saved": request.query_params.get("saved") == "1",
            "propagated": _parse_optional_int(request.query_params.get("propagated")),
            "propagate_scope": request.query_params.get("propagate_scope"),
        },
    )


@web_router.post("/user-groups/{group_id}")
async def update_user_group_form(
    group_id: int,
    request: Request,
    name: str = Form(...),
    is_active: str | None = Form(None),
    default_tier: str = Form(TIER_NORMAL),
    default_free_small_only: str | None = Form(None),
    free_small_daily_limit_enabled: str | None = Form(None),
    free_small_daily_limit: int = Form(0),
    default_allowed_endpoints: list[str] | None = Form(None),
    default_allowed_upstreams: list[str] | None = Form(None),
    default_anlas_total: int = Form(0),
    default_reset_period: str = Form("month"),
    default_reset_day: int = Form(1),
    propagate_scope: str = Form("unmodified"),
):
    if propagate_scope not in {"unmodified", "all", "none"}:
        propagate_scope = "unmodified"
    result = await patch_user_group(
        group_id,
        UpdateUserGroupRequest(
            name=name,
            is_active=is_active == "on",
            default_tier=default_tier,
            default_free_small_only=default_free_small_only == "on",
            free_small_daily_limit_enabled=free_small_daily_limit_enabled == "on",
            free_small_daily_limit=free_small_daily_limit,
            default_allowed_endpoints=default_allowed_endpoints or [],
            default_allowed_upstreams=default_allowed_upstreams or [],
            default_anlas_total=default_anlas_total,
            default_reset_period=default_reset_period,
            default_reset_day=default_reset_day,
            propagate=propagate_scope,
        ),
        request,
    )
    summary = result["propagation"]
    redirect_url = f"/admin/user-groups/{group_id}?saved=1"
    if propagate_scope != "none":
        redirect_url += f"&propagated={summary['updated_users']}&propagate_scope={propagate_scope}"
    return RedirectResponse(redirect_url, status_code=303)


@web_router.post("/user-groups/{group_id}/delete")
async def delete_user_group_form(group_id: int, request: Request):
    await delete_user_group(group_id, request)
    return RedirectResponse("/admin/user-groups", status_code=303)


@web_router.post("/user-groups/{group_id}/sync-members")
async def sync_user_group_members_form(
    group_id: int,
    request: Request,
    fields: list[str] | None = Form(None),
):
    await sync_user_group_members(group_id, SyncGroupMembersRequest(fields=fields or []), request)
    return RedirectResponse(f"/admin/user-groups/{group_id}", status_code=303)


@web_router.post("/user-groups/{group_id}/rate-limit-rules")
async def add_group_rate_limit_rule_form(
    group_id: int,
    request: Request,
    period: str = Form(...),
    max_requests: int = Form(...),
):
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
    await update_group_rate_limit_rule(
        rule_id,
        GroupRateLimitRuleRequest(period=period, max_requests=max_requests, is_active=is_active == "on"),
        request,
    )
    return RedirectResponse(f"/admin/user-groups/{group_id}", status_code=303)


@web_router.post("/group-rate-limit-rules/{rule_id}/delete")
async def delete_group_rate_limit_rule_form(rule_id: int, request: Request, group_id: int = Form(...)):
    await delete_group_rate_limit_rule(rule_id, request)
    return RedirectResponse(f"/admin/user-groups/{group_id}", status_code=303)


def _group_row_to_dict(row):
    data = row_to_dict(row)
    data["default_allowed_endpoints_list"] = AllowedEndpoints.parse(data.get("default_allowed_endpoints")).as_list()
    data["default_allowed_upstreams_list"] = AllowedUpstreams.parse(data.get("default_allowed_upstreams")).as_list()
    return data


def _parse_optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _normalize_update_reset_day_or_400(
    db: Database,
    group_id: int,
    payload: UpdateUserGroupRequest,
) -> int | None:
    if payload.default_reset_period is not None:
        return normalize_reset_day_or_400(payload.default_reset_period, payload.default_reset_day)
    if payload.default_reset_day is None:
        return None
    group = get_group(db, group_id)
    return normalize_reset_day_or_400(str(group["default_reset_period"]), payload.default_reset_day)


def _validate_update_free_small_daily_limit(
    db: Database,
    group_id: int,
    payload: UpdateUserGroupRequest,
) -> None:
    fields_set = payload.model_fields_set
    if "free_small_daily_limit_enabled" not in fields_set and "free_small_daily_limit" not in fields_set:
        return
    group = get_group(db, group_id)
    enabled = (
        bool(payload.free_small_daily_limit_enabled)
        if "free_small_daily_limit_enabled" in fields_set
        else bool(group["free_small_daily_limit_enabled"])
    )
    limit = (
        int(payload.free_small_daily_limit)
        if "free_small_daily_limit" in fields_set and payload.free_small_daily_limit is not None
        else int(group["free_small_daily_limit"] or 0)
    )
    validate_free_small_daily_limit(enabled, limit)


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
