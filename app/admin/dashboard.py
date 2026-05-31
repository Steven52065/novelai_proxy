from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..database import Database
from .auth import has_admin_session, require_admin_or_session
from .common import (
    DISPLAY_TIMEZONE,
    add_month,
    local_day_range,
    row_to_dict,
    templates,
    to_utc_iso,
)


api_router = APIRouter(prefix="/admin/api")
web_router = APIRouter(prefix="/admin")


@api_router.get("/queue", dependencies=[Depends(require_admin_or_session)])
async def queue_status(request: Request):
    return queue_status_payload(request)


@web_router.get("", response_class=HTMLResponse)
async def dashboard_alias(request: Request):
    return await dashboard(request)


@web_router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    db: Database = request.app.state.db
    today_start, today_end = local_day_range(datetime.now(DISPLAY_TIMEZONE))
    total_users = db.query_one("SELECT COUNT(*) AS c FROM users")["c"]
    today_requests = db.query_one(
        """
        SELECT COUNT(*) AS c
        FROM usage_logs
        WHERE datetime(created_at) >= datetime(?)
          AND datetime(created_at) < datetime(?)
        """,
        (to_utc_iso(today_start), to_utc_iso(today_end)),
    )["c"]
    total_anlas = db.query_one("SELECT COALESCE(SUM(final_anlas_cost), 0) AS c FROM usage_logs WHERE status = 'success'")["c"]
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
            "queue": queue_status_payload(request),
            "request_trends": _request_trend_stats(db),
        },
    )


def queue_status_payload(request: Request):
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


def _request_trend_stats(db: Database) -> dict:
    now = datetime.now(DISPLAY_TIMEZONE)
    today_start, today_end = local_day_range(now)
    week_start = today_start - timedelta(days=today_start.weekday())
    week_end = week_start + timedelta(days=7)
    month_start = today_start.replace(day=1)
    month_end = add_month(month_start)

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

    _fill_trend_range_from_rows(
        ranges["today"],
        db.query_all(
            """
            SELECT CAST(strftime('%H', datetime(created_at, '+8 hours')) AS INTEGER) AS bucket,
                   COUNT(*) AS requests,
                   SUM(CASE WHEN lower(status) = 'failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN lower(status) = 'rejected' THEN 1 ELSE 0 END) AS rejected
            FROM usage_logs
            WHERE datetime(created_at) >= datetime(?)
              AND datetime(created_at) < datetime(?)
            GROUP BY bucket
            """,
            (to_utc_iso(today_start), to_utc_iso(today_end)),
        ),
    )
    _fill_trend_range_from_rows(
        ranges["week"],
        _date_bucket_rows(db, week_start, week_end),
        _date_index_map(week_start, 7),
    )
    _fill_trend_range_from_rows(
        ranges["month"],
        _date_bucket_rows(db, month_start, month_end),
        _date_index_map(month_start, (month_end - month_start).days),
    )

    return ranges


def _date_bucket_rows(db: Database, start: datetime, end: datetime) -> list:
    return db.query_all(
        """
        SELECT date(datetime(created_at, '+8 hours')) AS bucket,
               COUNT(*) AS requests,
               SUM(CASE WHEN lower(status) = 'failed' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN lower(status) = 'rejected' THEN 1 ELSE 0 END) AS rejected
        FROM usage_logs
        WHERE datetime(created_at) >= datetime(?)
          AND datetime(created_at) < datetime(?)
        GROUP BY bucket
        """,
        (to_utc_iso(start), to_utc_iso(end)),
    )


def _date_index_map(start: datetime, bucket_count: int) -> dict[str, int]:
    return {
        (start + timedelta(days=index)).date().isoformat(): index
        for index in range(bucket_count)
    }


def _fill_trend_range_from_rows(trend_range: dict, rows: list, bucket_indexes: dict[str, int] | None = None) -> None:
    for row in rows:
        raw_bucket = row["bucket"]
        bucket_index = bucket_indexes.get(str(raw_bucket)) if bucket_indexes is not None else raw_bucket
        if bucket_index is None:
            continue
        try:
            index = int(bucket_index)
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(trend_range["labels"]):
            continue
        requests = int(row["requests"] or 0)
        failed = int(row["failed"] or 0)
        rejected = int(row["rejected"] or 0)
        trend_range["series"]["requests"][index] = requests
        trend_range["series"]["failed"][index] = failed
        trend_range["series"]["rejected"][index] = rejected
        trend_range["totals"]["requests"] += requests
        trend_range["totals"]["failed"] += failed
        trend_range["totals"]["rejected"] += rejected


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
    return {row["request_id"]: row_to_dict(row) for row in rows}


def _merge_queue_log_details(item: dict, details: dict[str, dict]) -> dict:
    merged = dict(item)
    merged.update(details.get(item["request_id"], {}))
    return merged
