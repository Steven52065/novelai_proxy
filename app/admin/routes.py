from __future__ import annotations

import hmac
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ..database import Database, utc_now_iso
from ..quota_manager import QuotaManager
from ..security import constant_time_equal, generate_api_key, hash_api_key


router = APIRouter(tags=["admin"])
api_router = APIRouter(prefix="/admin/api")
web_router = APIRouter(prefix="/admin")
security = HTTPBasic()
optional_security = HTTPBasic(auto_error=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
SESSION_COOKIE = "novelai_proxy_admin"
DISPLAY_TIMEZONE = timezone(timedelta(hours=8))
ALLOWED_ENDPOINT_CHOICES = {
    "generate-image": "图像生成",
    "suggest-tags": "标签建议",
    "upscale": "图片放大",
    "augment-image": "图像增强",
    "encode-vibe": "Vibe 编码",
}
DEFAULT_ALLOWED_ENDPOINTS = "generate-image"


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


class CleanupLogsRequest(BaseModel):
    older_than_days: int = Field(default=30, ge=0, le=3650)
    statuses: list[str] = Field(default_factory=list)


class ClearPayloadsRequest(BaseModel):
    older_than_days: int = Field(default=7, ge=0, le=3650)
    min_payload_kb: int = Field(default=128, ge=0, le=1024 * 1024)
    clear_output_files: bool = False


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
    if _has_admin_session(request):
        return
    if credentials is None:
        raise HTTPException(status_code=401, detail={"message": "Invalid admin credentials"})
    require_admin(request, credentials)


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
    return {"users": [_user_row_to_dict(row) for row in rows]}


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
                _serialize_allowed_endpoints(payload.allowed_endpoints),
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
        params.append(_serialize_allowed_endpoints(payload.allowed_endpoints))
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


@api_router.get("/logs", dependencies=[Depends(require_admin)])
async def logs(request: Request, user_id: int | None = None, limit: int = 100):
    db: Database = request.app.state.db
    limit = max(1, min(limit, 500))
    if user_id is None:
        rows = db.query_all(
            """
            SELECT l.*, u.name AS user_name
            FROM usage_logs l
            JOIN users u ON u.id = l.user_id
            ORDER BY l.id DESC
            LIMIT ?
            """,
            (limit,),
        )
    else:
        rows = db.query_all(
            """
            SELECT l.*, u.name AS user_name
            FROM usage_logs l
            JOIN users u ON u.id = l.user_id
            WHERE l.user_id = ?
            ORDER BY l.id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
    return {"logs": [_usage_log_to_dict(row) for row in rows]}


@api_router.get("/queue", dependencies=[Depends(require_admin_or_session)])
async def queue_status(request: Request):
    return _queue_status_payload(request)


@api_router.get("/database/stats", dependencies=[Depends(require_admin)])
async def database_stats(request: Request):
    return _database_stats(request)


@api_router.post("/database/cleanup-logs", dependencies=[Depends(require_admin)])
async def cleanup_logs(payload: CleanupLogsRequest, request: Request):
    return _cleanup_logs(request, payload.older_than_days, payload.statuses)


@api_router.post("/database/clear-payloads", dependencies=[Depends(require_admin)])
async def clear_payloads(payload: ClearPayloadsRequest, request: Request):
    return _clear_payloads(
        request,
        older_than_days=payload.older_than_days,
        min_payload_kb=payload.min_payload_kb,
        clear_output_files=payload.clear_output_files,
    )


@api_router.post("/database/vacuum", dependencies=[Depends(require_admin)])
async def vacuum_database(request: Request):
    return _vacuum_database(request)


def _queue_status_payload(request: Request):
    db: Database = request.app.state.db
    snapshot = request.app.state.proxy_queue.snapshot()
    request_ids = [
        item["request_id"]
        for item in ([snapshot["running"]] if snapshot["running"] else []) + snapshot["queued"]
    ]
    log_details = _queue_log_details(db, request_ids)
    if snapshot["running"]:
        snapshot["running"] = _merge_queue_log_details(snapshot["running"], log_details)
    snapshot["queued"] = [_merge_queue_log_details(item, log_details) for item in snapshot["queued"]]
    return snapshot


@web_router.get("", response_class=HTMLResponse)
async def dashboard_alias(request: Request):
    return await dashboard(request)


@web_router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not _has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    db: Database = request.app.state.db
    today = _today_utc_prefix()
    total_users = db.query_one("SELECT COUNT(*) AS c FROM users")["c"]
    today_requests = db.query_one("SELECT COUNT(*) AS c FROM usage_logs WHERE created_at >= ?", (today,))["c"]
    total_anlas = db.query_one("SELECT COALESCE(SUM(final_anlas_cost), 0) AS c FROM usage_logs WHERE status = 'success'")["c"]
    request_trends = _request_trend_stats(db)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active": "dashboard",
            "stats": {
                "total_users": total_users,
                "today_requests": today_requests,
                "total_anlas": total_anlas,
                "queue_size": request.app.state.proxy_queue.qsize(),
            },
            "queue": _queue_status_payload(request),
            "request_trends": request_trends,
        },
    )


@web_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@web_router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    config = request.app.state.config
    if not (constant_time_equal(username, config.admin.username) and constant_time_equal(password, config.admin.password)):
        return templates.TemplateResponse(request, "login.html", {"error": "用户名或密码错误"}, status_code=401)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(SESSION_COOKIE, _session_value(request), httponly=True, samesite="lax")
    return response


@web_router.post("/logout")
async def logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@web_router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    if not _has_admin_session(request):
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
            "users": [_user_row_to_dict(row) for row in rows],
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
    if not _has_admin_session(request):
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
    if not _has_admin_session(request):
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
            "user": _user_row_to_dict(user),
            "rules": [_row_to_dict(row) for row in rules],
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
    if not _has_admin_session(request):
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
    if not _has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await delete_user(user_id, request)
    return RedirectResponse("/admin/users", status_code=303)


@web_router.post("/users/{user_id}/reset-quota")
async def reset_quota_form(user_id: int, request: Request):
    if not _has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await reset_user_quota(user_id, request)
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@web_router.post("/users/{user_id}/reset-key")
async def reset_key_form(user_id: int, request: Request):
    if not _has_admin_session(request):
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
    if not _has_admin_session(request):
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
    if not _has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await update_rate_limit_rule(
        rule_id,
        RateLimitRuleRequest(period=period, max_requests=max_requests, is_active=is_active == "on"),
        request,
    )
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@web_router.post("/rate-limit-rules/{rule_id}/delete")
async def delete_rate_limit_rule_form(rule_id: int, request: Request, user_id: int = Form(...)):
    if not _has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    await delete_rate_limit_rule(rule_id, request)
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@web_router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, user_id: str | None = None, limit: int = 100):
    if not _has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    selected_user_id = _optional_query_int(user_id)
    data = await logs(request, user_id=selected_user_id, limit=limit)
    users = request.app.state.db.query_all("SELECT id, name FROM users ORDER BY name")
    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "active": "logs",
            "logs": data["logs"],
            "users": [_row_to_dict(row) for row in users],
            "selected_user_id": selected_user_id,
            "limit": limit,
        },
    )


@web_router.get("/database", response_class=HTMLResponse)
async def database_page(request: Request, message: str | None = None):
    if not _has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "database.html",
        {
            "active": "database",
            "stats": _database_stats(request),
            "message": message,
            "status_choices": ["success", "failed", "rejected", "queued", "running"],
        },
    )


@web_router.post("/database/cleanup-logs")
async def cleanup_logs_form(
    request: Request,
    older_than_days: int = Form(30),
    statuses: list[str] | None = Form(None),
):
    if not _has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    result = _cleanup_logs(request, older_than_days, statuses or [])
    return RedirectResponse(f"/admin/database?message=已删除 {result['deleted_logs']} 条日志记录", status_code=303)


@web_router.post("/database/clear-payloads")
async def clear_payloads_form(
    request: Request,
    older_than_days: int = Form(7),
    min_payload_kb: int = Form(128),
    clear_output_files: str | None = Form(None),
):
    if not _has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    result = _clear_payloads(
        request,
        older_than_days=older_than_days,
        min_payload_kb=min_payload_kb,
        clear_output_files=clear_output_files == "on",
    )
    return RedirectResponse(f"/admin/database?message=已清空 {result['updated_logs']} 条日志的大字段", status_code=303)


@web_router.post("/database/vacuum")
async def vacuum_database_form(request: Request):
    if not _has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    result = _vacuum_database(request)
    before = _format_bytes(result["before_bytes"])
    after = _format_bytes(result["after_bytes"])
    return RedirectResponse(f"/admin/database?message=数据库压缩完成：{before} -> {after}", status_code=303)


def _session_value(request: Request) -> str:
    config = request.app.state.config
    payload = config.admin.username
    signature = hmac.digest(config.admin.password.encode(), payload.encode(), "sha256").hex()
    return f"{payload}:{signature}"


def _has_admin_session(request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    return hmac.compare_digest(cookie, _session_value(request))


def _today_utc_prefix() -> str:
    return utc_now_iso().split("T", 1)[0]


def _request_trend_stats(db: Database) -> dict:
    now = datetime.now(DISPLAY_TIMEZONE)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)
    month_end = _add_month(month_start)

    ranges = {
        "today": _empty_trend_range(
            labels=[f"{hour:02d}:00" for hour in range(24)],
            bucket_count=24,
        ),
        "week": _empty_trend_range(
            labels=[
                f"{weekday} {((week_start + timedelta(days=index)).strftime('%m-%d'))}"
                for index, weekday in enumerate(("周一", "周二", "周三", "周四", "周五", "周六", "周日"))
            ],
            bucket_count=7,
        ),
        "month": _empty_trend_range(
            labels=[
                (month_start + timedelta(days=index)).strftime("%m-%d")
                for index in range((month_end - month_start).days)
            ],
            bucket_count=(month_end - month_start).days,
        ),
    }

    cutoff = min(today_start, week_start, month_start).astimezone(timezone.utc).isoformat()
    rows = db.query_all(
        """
        SELECT status, created_at
        FROM usage_logs
        WHERE created_at >= ?
        ORDER BY created_at
        """,
        (cutoff,),
    )
    for row in rows:
        created_at = _parse_log_datetime(row["created_at"])
        if created_at is None:
            continue
        local_time = created_at.astimezone(DISPLAY_TIMEZONE)
        status = str(row["status"] or "").lower()
        _add_trend_point(ranges["today"], local_time, status, today_start, today_start + timedelta(days=1), local_time.hour)
        _add_trend_point(ranges["week"], local_time, status, week_start, week_start + timedelta(days=7), local_time.weekday())
        _add_trend_point(ranges["month"], local_time, status, month_start, month_end, local_time.day - 1)

    return ranges


def _empty_trend_range(labels: list[str], bucket_count: int) -> dict:
    return {
        "labels": labels,
        "series": {
            "requests": [0 for _ in range(bucket_count)],
            "failed": [0 for _ in range(bucket_count)],
            "rejected": [0 for _ in range(bucket_count)],
        },
        "totals": {"requests": 0, "failed": 0, "rejected": 0},
    }


def _add_trend_point(
    trend_range: dict,
    local_time: datetime,
    status: str,
    start: datetime,
    end: datetime,
    bucket_index: int,
) -> None:
    if not (start <= local_time < end):
        return
    if bucket_index < 0 or bucket_index >= len(trend_range["labels"]):
        return
    trend_range["series"]["requests"][bucket_index] += 1
    trend_range["totals"]["requests"] += 1
    if status == "failed":
        trend_range["series"]["failed"][bucket_index] += 1
        trend_range["totals"]["failed"] += 1
    elif status == "rejected":
        trend_range["series"]["rejected"][bucket_index] += 1
        trend_range["totals"]["rejected"] += 1


def _parse_log_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _add_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1, day=1)
    return value.replace(month=value.month + 1, day=1)


def _row_to_dict(row):
    return {key: row[key] for key in row.keys()}


def _user_row_to_dict(row):
    data = _row_to_dict(row)
    data["allowed_endpoints_list"] = _parse_allowed_endpoints(data.get("allowed_endpoints"))
    data["allowed_endpoint_labels"] = [
        ALLOWED_ENDPOINT_CHOICES.get(endpoint, endpoint)
        for endpoint in data["allowed_endpoints_list"]
    ]
    return data


def _parse_allowed_endpoints(value: str | None) -> list[str]:
    if not value:
        return [DEFAULT_ALLOWED_ENDPOINTS]
    endpoints = [item.strip() for item in value.split(",") if item.strip()]
    return endpoints or [DEFAULT_ALLOWED_ENDPOINTS]


def _serialize_allowed_endpoints(value: list[str] | None) -> str:
    if not value:
        return DEFAULT_ALLOWED_ENDPOINTS
    valid = []
    for endpoint in value:
        if endpoint in ALLOWED_ENDPOINT_CHOICES and endpoint not in valid:
            valid.append(endpoint)
    return ",".join(valid or [DEFAULT_ALLOWED_ENDPOINTS])


def _usage_log_to_dict(row):
    data = _row_to_dict(row)
    data["created_at_display"] = _format_display_time(data.get("created_at"))
    data["completed_at_display"] = _format_display_time(data.get("completed_at"))
    data["request_payload"] = _json_or_none(data.get("request_payload"))
    data["output_files"] = _json_or_empty_list(data.get("output_files"))
    return data


def _format_display_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S UTC+8")


def _queue_log_details(db: Database, request_ids: list[str]) -> dict[str, dict]:
    if not request_ids:
        return {}
    placeholders = ",".join("?" for _ in request_ids)
    rows = db.query_all(
        f"""
        SELECT l.request_id, l.model, l.width, l.height, l.steps, l.n_samples,
               l.created_at, u.name AS user_name
        FROM usage_logs l
        JOIN users u ON u.id = l.user_id
        WHERE l.request_id IN ({placeholders})
        """,
        tuple(request_ids),
    )
    return {row["request_id"]: _row_to_dict(row) for row in rows}


def _merge_queue_log_details(item: dict, details: dict[str, dict]) -> dict:
    merged = dict(item)
    merged.update(details.get(item["request_id"], {}))
    return merged


def _json_or_none(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _json_or_empty_list(value):
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    return loaded if isinstance(loaded, list) else [loaded]


def _optional_query_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": "Invalid query parameter"}) from exc


def _database_stats(request: Request) -> dict:
    db: Database = request.app.state.db
    db_path = db.path
    db_files = _database_file_sizes(db_path)
    page = db.query_one("PRAGMA page_count")
    free = db.query_one("PRAGMA freelist_count")
    page_size = db.query_one("PRAGMA page_size")
    usage = db.query_one(
        """
        SELECT COUNT(*) AS total_logs,
               COALESCE(SUM(LENGTH(request_payload)), 0) AS request_payload_bytes,
               COALESCE(SUM(LENGTH(output_files)), 0) AS output_files_bytes,
               SUM(CASE WHEN request_payload IS NOT NULL AND request_payload != '' THEN 1 ELSE 0 END) AS logs_with_payload
        FROM usage_logs
        """
    )
    status_rows = db.query_all(
        """
        SELECT status, COUNT(*) AS count
        FROM usage_logs
        GROUP BY status
        ORDER BY count DESC, status
        """
    )
    largest_logs = db.query_all(
        """
        SELECT l.id, l.request_id, l.action, l.status, l.created_at, u.name AS user_name,
               COALESCE(LENGTH(l.request_payload), 0) AS request_payload_bytes,
               COALESCE(LENGTH(l.output_files), 0) AS output_files_bytes
        FROM usage_logs l
        JOIN users u ON u.id = l.user_id
        ORDER BY request_payload_bytes DESC, output_files_bytes DESC
        LIMIT 10
        """
    )
    total_payload_bytes = int(usage["request_payload_bytes"] or 0) + int(usage["output_files_bytes"] or 0)
    main_bytes = int(db_files["main"]["bytes"])
    wal_bytes = int(db_files["wal"]["bytes"])
    shm_bytes = int(db_files["shm"]["bytes"])
    page_count = int(page[0])
    freelist_count = int(free[0])
    sqlite_bytes = page_count * int(page_size[0])
    reclaimable_bytes = freelist_count * int(page_size[0])
    return {
        "database_path": str(db_path),
        "files": db_files,
        "total_file_bytes": main_bytes + wal_bytes + shm_bytes,
        "main_file_bytes": main_bytes,
        "sqlite_bytes": sqlite_bytes,
        "reclaimable_bytes": reclaimable_bytes,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "page_size": int(page_size[0]),
        "usage_logs": {
            "total": int(usage["total_logs"] or 0),
            "logs_with_payload": int(usage["logs_with_payload"] or 0),
            "request_payload_bytes": int(usage["request_payload_bytes"] or 0),
            "output_files_bytes": int(usage["output_files_bytes"] or 0),
            "payload_bytes": total_payload_bytes,
        },
        "status_counts": [_row_to_dict(row) for row in status_rows],
        "largest_logs": [_usage_log_size_to_dict(row) for row in largest_logs],
    }


def _cleanup_logs(request: Request, older_than_days: int, statuses: list[str]) -> dict:
    db: Database = request.app.state.db
    cutoff = _cutoff_time(older_than_days)
    valid_statuses = _valid_statuses(statuses)
    where = ["created_at < ?"]
    params: list[object] = [cutoff]
    if valid_statuses:
        where.append(f"status IN ({','.join('?' for _ in valid_statuses)})")
        params.extend(valid_statuses)
    cursor = db.execute(f"DELETE FROM usage_logs WHERE {' AND '.join(where)}", tuple(params))
    return {"ok": True, "deleted_logs": int(cursor.rowcount), "cutoff": cutoff, "statuses": valid_statuses}


def _clear_payloads(
    request: Request,
    *,
    older_than_days: int,
    min_payload_kb: int,
    clear_output_files: bool,
) -> dict:
    db: Database = request.app.state.db
    cutoff = _cutoff_time(older_than_days)
    min_payload_bytes = min_payload_kb * 1024
    set_clause = "request_payload = NULL"
    if clear_output_files:
        set_clause += ", output_files = NULL"
    cursor = db.execute(
        f"""
        UPDATE usage_logs
        SET {set_clause}
        WHERE created_at < ?
          AND COALESCE(LENGTH(request_payload), 0) >= ?
        """,
        (cutoff, min_payload_bytes),
    )
    return {
        "ok": True,
        "updated_logs": int(cursor.rowcount),
        "cutoff": cutoff,
        "min_payload_bytes": min_payload_bytes,
        "clear_output_files": clear_output_files,
    }


def _vacuum_database(request: Request) -> dict:
    db: Database = request.app.state.db
    before = _database_total_size(db.path)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("VACUUM")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    after = _database_total_size(db.path)
    return {"ok": True, "before_bytes": before, "after_bytes": after, "reclaimed_bytes": max(before - after, 0)}


def _cutoff_time(older_than_days: int) -> str:
    days = max(0, min(int(older_than_days), 3650))
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _valid_statuses(statuses: list[str]) -> list[str]:
    allowed = {"success", "failed", "rejected", "queued", "running"}
    valid = []
    for status in statuses:
        if status in allowed and status not in valid:
            valid.append(status)
    return valid


def _database_file_sizes(path: Path) -> dict[str, dict]:
    files = {
        "main": path,
        "wal": Path(f"{path}-wal"),
        "shm": Path(f"{path}-shm"),
    }
    return {
        key: {
            "path": str(file_path),
            "bytes": file_path.stat().st_size if file_path.exists() else 0,
            "display": _format_bytes(file_path.stat().st_size if file_path.exists() else 0),
        }
        for key, file_path in files.items()
    }


def _database_total_size(path: Path) -> int:
    return sum(file_info["bytes"] for file_info in _database_file_sizes(path).values())


def _usage_log_size_to_dict(row):
    data = _row_to_dict(row)
    request_payload_bytes = int(data["request_payload_bytes"] or 0)
    output_files_bytes = int(data["output_files_bytes"] or 0)
    data["created_at_display"] = _format_display_time(data.get("created_at"))
    data["request_payload_display"] = _format_bytes(request_payload_bytes)
    data["output_files_display"] = _format_bytes(output_files_bytes)
    data["total_bytes"] = request_payload_bytes + output_files_bytes
    data["total_display"] = _format_bytes(data["total_bytes"])
    return data


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024


router.include_router(api_router)
router.include_router(web_router)
