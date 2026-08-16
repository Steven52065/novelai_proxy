from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..allowlists import ALLOWED_ENDPOINT_CHOICES, DEFAULT_ALLOWED_ENDPOINTS
from ..api_key_flash import ApiKeyFlashStore
from ..cookies import delete_response_cookie
from ..database import Database, utc_now_iso
from ..image_format_policies import (
    DEFAULT_IMAGE_FORMAT_POLICY,
    IMAGE_FORMAT_POLICY_CHOICES,
    ImageFormatPolicy,
)
from ..queue_tiers import TIER_NORMAL, USER_TIER_PATTERN
from ..templating import templates
from ..users import (
    CreateUserInput,
    UpdateUserInput,
    create_user as create_user_record,
    delete_user as delete_user_record,
    ensure_user_exists,
    get_enabled_group,
    group_defaults,
    list_groups,
    load_group_member_rate_limit_rules,
    reset_api_key,
    update_user as update_user_record,
)
from .auth import require_admin_or_session, require_admin_page_session
from .common import (
    format_display_time,
    normalize_image_format_policy_or_400,
    normalize_reset_day_or_400,
    notify_dashboard_change,
    optional_form_int,
    row_to_dict,
    upstream_choices,
    user_row_to_dict,
    validate_allowed_endpoints,
    validate_allowed_upstreams,
    validate_free_small_daily_limit,
)


api_router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin_or_session)])
web_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_page_session)])
API_KEY_FLASH_COOKIE = "novelai_proxy_api_key_flash"
_api_key_flash = ApiKeyFlashStore(cookie_name=API_KEY_FLASH_COOKIE, state_attr="admin_api_key_flash_store")
USERS_PAGE_SIZE = 50
USER_PAGE_SORT_COLUMNS = {
    "id": "u.id",
    "name": "u.name COLLATE NOCASE",
    "anlas": "COALESCE(q.used, 0) + COALESCE(q.reserved, 0)",
}


class CreateUserRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    group_id: int | None = Field(default=None, ge=1)
    tier: str = Field(default=TIER_NORMAL, pattern=USER_TIER_PATTERN)
    free_small_only: bool = False
    free_small_daily_limit_enabled: bool = False
    free_small_daily_limit: int = Field(default=0, ge=0)
    allowed_endpoints: list[str] = Field(default_factory=lambda: [DEFAULT_ALLOWED_ENDPOINTS])
    allowed_upstreams: list[str] = Field(default_factory=list)
    image_format_policy: ImageFormatPolicy = DEFAULT_IMAGE_FORMAT_POLICY
    anlas_total: int = Field(default=0, ge=0)
    reset_period: str = Field(default="month", pattern="^(month|week|day|never)$")
    reset_day: int | None = Field(default=None, ge=0, le=28)


class UpdateUserRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    group_id: int | None = Field(default=None, ge=1)
    apply_group_defaults: bool = False
    tier: str | None = Field(default=None, pattern=USER_TIER_PATTERN)
    is_active: bool | None = None
    free_small_only: bool | None = None
    free_small_daily_limit_enabled: bool | None = None
    free_small_daily_limit: int | None = Field(default=None, ge=0)
    allowed_endpoints: list[str] | None = None
    allowed_upstreams: list[str] | None = None
    image_format_policy: ImageFormatPolicy | None = None
    anlas_total: int | None = Field(default=None, ge=0)
    reset_period: str | None = Field(default=None, pattern="^(month|week|day|never)$")
    reset_day: int | None = Field(default=None, ge=0, le=28)


class RateLimitRuleRequest(BaseModel):
    period: str = Field(..., pattern="^(minute|hour|day|month)$")
    max_requests: int = Field(..., ge=1)
    is_active: bool = True


class BulkUserIdsRequest(BaseModel):
    user_ids: list[int] = Field(..., min_length=1)


@api_router.get("/users")
async def list_users(request: Request):
    db: Database = request.app.state.db
    rows = db.query_all(
        """
        SELECT u.id, u.name, u.group_id, g.name AS group_name, g.is_active AS group_is_active,
               u.tier, u.is_active, u.free_small_only,
               u.free_small_daily_limit_enabled, u.free_small_daily_limit,
               u.allowed_endpoints, u.allowed_upstreams, u.image_format_policy, u.created_at,
               NULL AS api_key,
               COALESCE(q.total, 0) AS anlas_total,
               COALESCE(q.used, 0) AS anlas_used,
               COALESCE(q.reserved, 0) AS anlas_reserved
        FROM users u
        LEFT JOIN user_anlas_quota q ON q.user_id = u.id
        LEFT JOIN user_groups g ON g.id = u.group_id
        WHERE u.deleted_at IS NULL
        ORDER BY u.id DESC
        """
    )
    return {"users": [user_row_to_dict(row) for row in rows]}


@api_router.get("/users/search")
async def search_users(request: Request, q: str = "", limit: int = 20):
    db: Database = request.app.state.db
    normalized_limit = max(1, min(int(limit), 50))
    query = q.strip()
    params: list[object] = []
    where = "deleted_at IS NULL"
    if query:
        where += " AND name LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(query)}%")
    params.append(normalized_limit)
    rows = db.query_all(
        f"""
        SELECT id, name
        FROM users
        WHERE {where}
        ORDER BY name COLLATE NOCASE, id DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return {"users": [row_to_dict(row) for row in rows], "q": query, "limit": normalized_limit}


@api_router.post("/users")
async def create_user(payload: CreateUserRequest, request: Request):
    db: Database = request.app.state.db
    create_input = _build_create_user_input(db, payload)
    validate_allowed_endpoints(create_input.allowed_endpoints)
    validate_allowed_upstreams(create_input.allowed_upstreams, request)
    created = create_user_record(
        db,
        request.app.state.quota_manager,
        create_input,
    )
    notify_dashboard_change(request)
    return {"user_id": created.user_id, "api_key": created.api_key}


@api_router.post("/users/bulk-reset-anlas")
async def bulk_reset_anlas(payload: BulkUserIdsRequest, request: Request):
    db: Database = request.app.state.db
    user_ids = _normalize_bulk_user_ids(db, payload.user_ids)
    quota_manager = request.app.state.quota_manager
    for user_id in user_ids:
        # 额外重置：不更新 last_reset_at，保持原有自动重置周期不变。
        quota_manager.reset_usage(user_id, update_last_reset_at=False)
    notify_dashboard_change(request)
    return {"ok": True, "user_ids": user_ids}


@api_router.post("/users/bulk-reset-free-small-daily")
async def bulk_reset_free_small_daily(payload: BulkUserIdsRequest, request: Request):
    db: Database = request.app.state.db
    user_ids = _normalize_bulk_user_ids(db, payload.user_ids)
    manager = request.app.state.free_small_daily_limit_manager
    for user_id in user_ids:
        manager.reset_usage(user_id)
    notify_dashboard_change(request)
    return {"ok": True, "user_ids": user_ids}


@api_router.patch("/users/{user_id}")
async def update_user(user_id: int, payload: UpdateUserRequest, request: Request):
    db: Database = request.app.state.db
    ensure_user_exists(db, user_id)
    update_input = _build_update_user_input(db, user_id, payload)
    if update_input.allowed_endpoints is not None:
        validate_allowed_endpoints(update_input.allowed_endpoints)
    if update_input.allowed_upstreams is not None:
        validate_allowed_upstreams(update_input.allowed_upstreams, request)
    changed = update_user_record(
        db,
        request.app.state.quota_manager,
        user_id,
        update_input,
    )
    if changed:
        notify_dashboard_change(request)
    return {"ok": True}


@api_router.delete("/users/{user_id}")
async def delete_user(user_id: int, request: Request):
    db: Database = request.app.state.db
    delete_user_record(db, user_id)
    notify_dashboard_change(request)
    return {"ok": True}


@api_router.post("/users/{user_id}/reset-quota")
async def reset_user_quota(user_id: int, request: Request):
    ensure_user_exists(request.app.state.db, user_id)
    request.app.state.quota_manager.reset_usage(user_id)
    notify_dashboard_change(request)
    return {"ok": True}


@api_router.post("/users/{user_id}/reset-key")
async def reset_user_key(user_id: int, request: Request):
    db: Database = request.app.state.db
    api_key = reset_api_key(db, user_id)
    return {"user_id": user_id, "api_key": api_key}


@api_router.post("/users/{user_id}/rate-limit-rules")
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


@api_router.patch("/rate-limit-rules/{rule_id}")
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


@api_router.delete("/rate-limit-rules/{rule_id}")
async def delete_rate_limit_rule(rule_id: int, request: Request):
    db: Database = request.app.state.db
    _ensure_rate_limit_rule_exists(db, rule_id)
    db.execute("DELETE FROM rate_limit_rules WHERE id = ?", (rule_id,))
    return {"ok": True}


@web_router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    db: Database = request.app.state.db
    new_api_key = _api_key_flash.pop_flash(request)
    query = request.query_params.get("q", "").strip()
    group_filter = request.query_params.get("group_id", "").strip()
    tier_filter = request.query_params.get("tier", "").strip()
    status_filter = request.query_params.get("status", "").strip()
    sort_key = request.query_params.get("sort", "id").strip()
    direction = request.query_params.get("direction", "asc").strip().lower()

    if group_filter != "none":
        try:
            group_id = int(group_filter) if group_filter else None
        except ValueError:
            group_id = None
        if group_id is None and group_filter:
            group_filter = ""
        elif group_id is not None and group_id < 1:
            group_filter = ""
    if tier_filter not in {"", "normal", "vip"}:
        tier_filter = ""
    if status_filter not in {"", "active", "inactive"}:
        status_filter = ""
    if sort_key not in USER_PAGE_SORT_COLUMNS:
        sort_key = "id"
    if direction not in {"asc", "desc"}:
        direction = "asc"
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1

    where = ["u.deleted_at IS NULL"]
    params: list[object] = []
    if query:
        where.append("u.name LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(query)}%")
    if group_filter == "none":
        where.append("u.group_id IS NULL")
    elif group_filter:
        where.append("u.group_id = ?")
        params.append(int(group_filter))
    if tier_filter:
        where.append("u.tier = ?")
        params.append(tier_filter)
    if status_filter == "active":
        where.append("u.is_active = 1")
    elif status_filter == "inactive":
        where.append("u.is_active = 0")

    where_sql = " AND ".join(where)
    total = int(db.query_one(f"SELECT COUNT(*) AS total FROM users u WHERE {where_sql}", tuple(params))["total"])
    total_pages = max(1, (total + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)
    page = min(page, total_pages)
    offset = (page - 1) * USERS_PAGE_SIZE
    rows = db.query_all(
        f"""
        SELECT u.id, u.name, u.group_id, g.name AS group_name, g.is_active AS group_is_active,
               u.tier, u.is_active, u.free_small_only,
               u.free_small_daily_limit_enabled, u.free_small_daily_limit,
               u.allowed_endpoints, u.allowed_upstreams, u.image_format_policy, u.created_at,
               NULL AS api_key,
               COALESCE(q.total, 0) AS anlas_total,
               COALESCE(q.used, 0) AS anlas_used,
               COALESCE(q.reserved, 0) AS anlas_reserved,
               COALESCE(q.reset_period, 'month') AS reset_period,
               COALESCE(q.reset_day, 1) AS reset_day
        FROM users u
        LEFT JOIN user_anlas_quota q ON q.user_id = u.id
        LEFT JOIN user_groups g ON g.id = u.group_id
        WHERE {where_sql}
        ORDER BY {USER_PAGE_SORT_COLUMNS[sort_key]} {direction}, u.id ASC
        LIMIT ? OFFSET ?
        """,
        (*params, USERS_PAGE_SIZE, offset),
    )
    users = [user_row_to_dict(row) for row in rows]
    _attach_free_small_daily_snapshots(request, users)
    user_list_query = urlencode(
        {
            "q": query,
            "group_id": group_filter,
            "tier": tier_filter,
            "status": status_filter,
            "sort": sort_key,
            "direction": direction,
        }
    )
    response = templates.TemplateResponse(
        request,
        "users.html",
        {
            "active": "users",
            "users": users,
            "user_filters": {
                "q": query,
                "group_id": group_filter,
                "tier": tier_filter,
                "status": status_filter,
            },
            "user_sort": sort_key,
            "user_direction": direction,
            "user_page": page,
            "user_total": total,
            "user_total_pages": total_pages,
            "user_list_query": user_list_query,
            "groups": _groups_for_select(db, active_only=True),
            "endpoint_choices": ALLOWED_ENDPOINT_CHOICES,
            "upstream_choices": upstream_choices(request),
            "image_format_policy_choices": IMAGE_FORMAT_POLICY_CHOICES,
            "new_api_key": new_api_key,
        },
    )
    if new_api_key is not None:
        delete_response_cookie(response, request, API_KEY_FLASH_COOKIE)
    return response


@web_router.post("/users")
async def create_user_form(
    request: Request,
    name: str = Form(...),
    tier: str = Form(TIER_NORMAL),
    anlas_total: int = Form(0),
    reset_period: str = Form("month"),
    reset_day: int | None = Form(None),
    free_small_only: str | None = Form(None),
    free_small_daily_limit_enabled: str | None = Form(None),
    free_small_daily_limit: int = Form(0),
    allowed_endpoints: list[str] | None = Form(None),
    allowed_upstreams: list[str] | None = Form(None),
    image_format_policy: str = Form(DEFAULT_IMAGE_FORMAT_POLICY),
    group_id: str | None = Form(None),
    use_group_defaults: str | None = Form(None),
):
    parsed_group_id = optional_form_int(group_id)
    if use_group_defaults == "on" and parsed_group_id is not None:
        payload = CreateUserRequest(name=name, group_id=parsed_group_id)
    else:
        payload = CreateUserRequest(
            name=name,
            group_id=parsed_group_id,
            tier=tier,
            anlas_total=anlas_total,
            reset_period=reset_period,
            reset_day=reset_day,
            free_small_only=free_small_only == "on",
            free_small_daily_limit_enabled=free_small_daily_limit_enabled == "on",
            free_small_daily_limit=free_small_daily_limit,
            allowed_endpoints=allowed_endpoints or [],
            allowed_upstreams=allowed_upstreams or [],
            image_format_policy=normalize_image_format_policy_or_400(image_format_policy),
        )
    result = await create_user(
        payload,
        request,
    )
    response = RedirectResponse("/admin/users", status_code=303)
    _api_key_flash.set_flash(response, request, result["api_key"])
    return response


# 注意：批量路由必须先于 POST /users/{user_id} 注册，否则会被路径参数路由捕获。
@web_router.post("/users/bulk-reset-anlas")
async def bulk_reset_anlas_form(request: Request, user_ids: list[int] | None = Form(None)):
    if not user_ids:
        raise HTTPException(status_code=400, detail={"message": "No users selected"})
    await bulk_reset_anlas(BulkUserIdsRequest(user_ids=user_ids), request)
    return RedirectResponse("/admin/users", status_code=303)


@web_router.post("/users/bulk-reset-free-small-daily")
async def bulk_reset_free_small_daily_form(request: Request, user_ids: list[int] | None = Form(None)):
    if not user_ids:
        raise HTTPException(status_code=400, detail={"message": "No users selected"})
    await bulk_reset_free_small_daily(BulkUserIdsRequest(user_ids=user_ids), request)
    return RedirectResponse("/admin/users", status_code=303)


@web_router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_edit_page(user_id: int, request: Request):
    db: Database = request.app.state.db
    new_api_key = _api_key_flash.pop_flash(request)
    user = db.query_one(
        """
        SELECT u.id, u.name, u.group_id, g.name AS group_name, g.is_active AS group_is_active,
               u.tier, u.is_active, u.disabled_by_discord_verification, u.free_small_only,
               u.free_small_daily_limit_enabled, u.free_small_daily_limit,
               u.allowed_endpoints, u.allowed_upstreams, u.image_format_policy, NULL AS api_key,
               q.total AS anlas_total, q.used AS anlas_used, q.reserved AS anlas_reserved,
               q.reset_period, q.reset_day
        FROM users u
        LEFT JOIN user_anlas_quota q ON q.user_id = u.id
        LEFT JOIN user_groups g ON g.id = u.group_id
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
    recent_logs = [
        _recent_log_to_dict(row)
        for row in db.query_all(
            """
            SELECT id, action, status, total_ms, created_at
            FROM usage_logs
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (user_id,),
        )
    ]
    user_data = user_row_to_dict(user)
    user_data["free_small_daily"] = request.app.state.free_small_daily_limit_manager.get_snapshot(user_id)
    response = templates.TemplateResponse(
        request,
        "user_edit.html",
        {
            "active": "users",
            "user": user_data,
            "rules": [row_to_dict(row) for row in rules],
            "recent_logs": recent_logs,
            "groups": _groups_for_select(db, active_only=False),
            "endpoint_choices": ALLOWED_ENDPOINT_CHOICES,
            "upstream_choices": upstream_choices(request),
            "image_format_policy_choices": IMAGE_FORMAT_POLICY_CHOICES,
            "new_api_key": new_api_key,
        },
    )
    if new_api_key is not None:
        delete_response_cookie(response, request, API_KEY_FLASH_COOKIE)
    return response


def _recent_log_to_dict(row) -> dict:
    data = row_to_dict(row)
    data["created_at_display"] = format_display_time(data.get("created_at"))
    return data


@web_router.post("/users/{user_id}")
async def update_user_form(
    user_id: int,
    request: Request,
    name: str = Form(...),
    tier: str = Form(TIER_NORMAL),
    is_active: str | None = Form(None),
    hard_disable: str | None = Form(None),
    anlas_total: int = Form(0),
    reset_period: str = Form("month"),
    reset_day: int = Form(1),
    free_small_only: str | None = Form(None),
    free_small_daily_limit_enabled: str | None = Form(None),
    free_small_daily_limit: int = Form(0),
    allowed_endpoints: list[str] | None = Form(None),
    allowed_upstreams: list[str] | None = Form(None),
    image_format_policy: str = Form(DEFAULT_IMAGE_FORMAT_POLICY),
    group_id: str | None = Form(None),
    apply_group_defaults: str | None = Form(None),
):
    parsed_group_id = optional_form_int(group_id)
    db = request.app.state.db
    current_group_id = _get_user_group_id(db, user_id)
    submitted_is_active = is_active == "on"
    payload_data: dict[str, object] = {"name": name}
    # 编辑表单每次保存都会原样回传当前启用状态，只有与当前值不同才算管理员改动了启用状态。
    # 否则管理员改个名字就会清除“由 Discord 验证停用”标记，把自动恢复退化成永久停用。
    # 想在不改动启用状态的前提下确认封禁，走下面显式的“永久停用”开关。
    if submitted_is_active != _get_user_is_active(db, user_id):
        payload_data["is_active"] = submitted_is_active
    elif not submitted_is_active and hard_disable == "on":
        payload_data["is_active"] = False
    if apply_group_defaults == "on":
        payload_data.update(
            {
                "group_id": parsed_group_id,
                "apply_group_defaults": True,
            }
        )
    else:
        if parsed_group_id != current_group_id:
            payload_data["group_id"] = parsed_group_id
        payload_data.update(
            {
                "tier": tier,
                "free_small_only": free_small_only == "on",
                "free_small_daily_limit_enabled": free_small_daily_limit_enabled == "on",
                "free_small_daily_limit": free_small_daily_limit,
                "anlas_total": anlas_total,
                "reset_period": reset_period,
                "reset_day": reset_day,
                "allowed_endpoints": allowed_endpoints or [],
                "allowed_upstreams": allowed_upstreams or [],
                "image_format_policy": normalize_image_format_policy_or_400(image_format_policy),
            }
        )
    await update_user(
        user_id,
        UpdateUserRequest(**payload_data),
        request,
    )
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@web_router.post("/users/{user_id}/delete")
async def delete_user_form(user_id: int, request: Request):
    await delete_user(user_id, request)
    return RedirectResponse("/admin/users", status_code=303)


@web_router.post("/users/{user_id}/reset-quota")
async def reset_quota_form(user_id: int, request: Request):
    await reset_user_quota(user_id, request)
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@web_router.post("/users/{user_id}/reset-key")
async def reset_key_form(user_id: int, request: Request):
    result = await reset_user_key(user_id, request)
    response = RedirectResponse(f"/admin/users/{user_id}", status_code=303)
    _api_key_flash.set_flash(response, request, result["api_key"])
    return response


@web_router.post("/users/{user_id}/rate-limit-rules")
async def add_rate_limit_rule_form(
    user_id: int,
    request: Request,
    period: str = Form(...),
    max_requests: int = Form(...),
):
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
    await update_rate_limit_rule(
        rule_id,
        RateLimitRuleRequest(period=period, max_requests=max_requests, is_active=is_active == "on"),
        request,
    )
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@web_router.post("/rate-limit-rules/{rule_id}/delete")
async def delete_rate_limit_rule_form(rule_id: int, request: Request, user_id: int = Form(...)):
    await delete_rate_limit_rule(rule_id, request)
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


def _build_create_user_input(db: Database, payload: CreateUserRequest) -> CreateUserInput:
    defaults = None
    if payload.group_id is not None:
        defaults = group_defaults(get_enabled_group(db, payload.group_id))
    fields_set = payload.model_fields_set

    def value(field_name: str):
        if field_name in fields_set or defaults is None:
            return getattr(payload, field_name)
        return defaults[field_name]

    free_small_daily_limit_enabled = bool(value("free_small_daily_limit_enabled"))
    free_small_daily_limit = int(value("free_small_daily_limit"))
    validate_free_small_daily_limit(free_small_daily_limit_enabled, free_small_daily_limit)
    reset_period = str(value("reset_period"))
    reset_day = normalize_reset_day_or_400(reset_period, value("reset_day"))
    # 新建成员默认继承组的「用户独享限流」模板。
    rate_limit_rules = (
        load_group_member_rate_limit_rules(db, payload.group_id) if payload.group_id is not None else None
    )
    return CreateUserInput(
        name=payload.name,
        group_id=payload.group_id,
        tier=str(value("tier")),
        free_small_only=bool(value("free_small_only")),
        free_small_daily_limit_enabled=free_small_daily_limit_enabled,
        free_small_daily_limit=free_small_daily_limit,
        allowed_endpoints=list(value("allowed_endpoints")),
        allowed_upstreams=list(value("allowed_upstreams")),
        image_format_policy=normalize_image_format_policy_or_400(value("image_format_policy")),
        anlas_total=int(value("anlas_total")),
        reset_period=reset_period,
        reset_day=reset_day,
        rate_limit_rules=rate_limit_rules,
    )


def _build_update_user_input(db: Database, user_id: int, payload: UpdateUserRequest) -> UpdateUserInput:
    fields_set = payload.model_fields_set
    update_group_id = "group_id" in fields_set
    if update_group_id and payload.group_id is not None:
        get_enabled_group(db, payload.group_id)

    defaults = None
    default_rate_limit_rules = None
    if payload.apply_group_defaults:
        default_group_id = payload.group_id if update_group_id else _get_user_group_id(db, user_id)
        if default_group_id is None:
            raise HTTPException(status_code=400, detail={"message": "No user group selected"})
        defaults = group_defaults(get_enabled_group(db, int(default_group_id)))
        # 套用组默认值时，限频规则也整体拉回组模板。
        default_rate_limit_rules = load_group_member_rate_limit_rules(db, int(default_group_id))

    def optional_value(field_name: str):
        if field_name in fields_set:
            return getattr(payload, field_name)
        if defaults is not None:
            return defaults[field_name]
        return None

    free_small_daily_limit_enabled = optional_value("free_small_daily_limit_enabled")
    free_small_daily_limit = optional_value("free_small_daily_limit")
    _validate_update_free_small_daily_limit(db, user_id, free_small_daily_limit_enabled, free_small_daily_limit)
    allowed_endpoints = optional_value("allowed_endpoints")
    allowed_upstreams = optional_value("allowed_upstreams")
    image_format_policy = optional_value("image_format_policy")
    reset_period_value = optional_value("reset_period")
    reset_period = str(reset_period_value) if reset_period_value is not None else None
    reset_day = optional_value("reset_day")
    if reset_period is not None and reset_day is not None:
        reset_day = normalize_reset_day_or_400(str(reset_period), reset_day)
    elif reset_period is None and reset_day is not None:
        reset_day = normalize_reset_day_or_400(_get_user_reset_period(db, user_id), reset_day)
    return UpdateUserInput(
        name=payload.name if "name" in fields_set else None,
        group_id=payload.group_id,
        update_group_id=update_group_id,
        tier=optional_value("tier"),
        is_active=payload.is_active if "is_active" in fields_set else None,
        free_small_only=optional_value("free_small_only"),
        free_small_daily_limit_enabled=(
            bool(free_small_daily_limit_enabled) if free_small_daily_limit_enabled is not None else None
        ),
        free_small_daily_limit=int(free_small_daily_limit) if free_small_daily_limit is not None else None,
        allowed_endpoints=list(allowed_endpoints) if allowed_endpoints is not None else None,
        allowed_upstreams=list(allowed_upstreams) if allowed_upstreams is not None else None,
        image_format_policy=(
            normalize_image_format_policy_or_400(image_format_policy)
            if image_format_policy is not None
            else None
        ),
        anlas_total=optional_value("anlas_total"),
        reset_period=reset_period,
        reset_day=reset_day,
        rate_limit_rules=default_rate_limit_rules,
    )


def _normalize_bulk_user_ids(db: Database, user_ids: list[int]) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for user_id in user_ids:
        uid = int(user_id)
        if uid in seen:
            continue
        seen.add(uid)
        ensure_user_exists(db, uid)
        normalized.append(uid)
    return normalized


def _get_user_group_id(db: Database, user_id: int) -> int | None:
    row = db.query_one("SELECT group_id FROM users WHERE id = ? AND deleted_at IS NULL", (user_id,))
    if row is None or row["group_id"] is None:
        return None
    return int(row["group_id"])


def _get_user_is_active(db: Database, user_id: int) -> bool:
    row = db.query_one("SELECT is_active FROM users WHERE id = ? AND deleted_at IS NULL", (user_id,))
    return row is not None and bool(row["is_active"])


def _get_user_reset_period(db: Database, user_id: int) -> str:
    row = db.query_one("SELECT reset_period FROM user_anlas_quota WHERE user_id = ?", (user_id,))
    if row is None:
        return "month"
    return str(row["reset_period"])


def _validate_update_free_small_daily_limit(
    db: Database,
    user_id: int,
    enabled: bool | None,
    limit: int | None,
) -> None:
    if enabled is None and limit is None:
        return
    row = db.query_one(
        "SELECT free_small_daily_limit_enabled, free_small_daily_limit FROM users WHERE id = ? AND deleted_at IS NULL",
        (user_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail={"message": "User not found"})
    effective_enabled = bool(enabled) if enabled is not None else bool(row["free_small_daily_limit_enabled"])
    effective_limit = int(limit) if limit is not None else int(row["free_small_daily_limit"] or 0)
    validate_free_small_daily_limit(effective_enabled, effective_limit)


def _groups_for_select(db: Database, active_only: bool) -> list[dict]:
    groups = [row_to_dict(row) for row in list_groups(db)]
    if active_only:
        return [group for group in groups if int(group["is_active"])]
    return groups


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _attach_free_small_daily_snapshots(request: Request, users: list[dict]) -> None:
    manager = request.app.state.free_small_daily_limit_manager
    for user in users:
        user["free_small_daily"] = manager.get_snapshot(int(user["id"]))


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
