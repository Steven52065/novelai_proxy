from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

from ..database import Database
from ..logging_utils import logger
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
DASHBOARD_WS_HEARTBEAT_SECONDS = 30.0


@api_router.get("/queue", dependencies=[Depends(require_admin_or_session)])
async def queue_status(request: Request, upstream_id: str | None = None):
    return queue_status_payload(request, upstream_id=_normalize_upstream_filter(request, upstream_id))


@api_router.get("/dashboard", dependencies=[Depends(require_admin_or_session)])
async def dashboard_snapshot(
    request: Request,
    queue_upstream_id: str | None = None,
    trend_upstream_id: str | None = None,
    include_trends: bool = False,
):
    return dashboard_snapshot_payload(
        request,
        queue_upstream_id=_normalize_upstream_filter(request, queue_upstream_id),
        trend_upstream_id=_normalize_upstream_filter(request, trend_upstream_id),
        include_trends=include_trends,
    )


@api_router.get("/request-trends", dependencies=[Depends(require_admin_or_session)])
async def request_trends(request: Request, upstream_id: str | None = None):
    db: Database = request.app.state.db
    return _request_trend_stats(db, upstream_id=_normalize_upstream_filter(request, upstream_id))


@api_router.get("/stats", dependencies=[Depends(require_admin_or_session)])
async def admin_stats(request: Request):
    return _dashboard_stats(request)


@api_router.get("/upstream-weights", dependencies=[Depends(require_admin_or_session)])
async def upstream_weights(request: Request):
    return _upstream_weights_payload(request)


@web_router.get("", response_class=HTMLResponse)
async def dashboard_alias(request: Request):
    return await dashboard(request)


@web_router.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket):
    if not _is_same_origin_websocket(websocket):
        logger.warning(
            "dashboard websocket rejected by origin check origin=%s host=%s forwarded_host=%s forwarded_proto=%s url=%s",
            websocket.headers.get("origin"),
            websocket.headers.get("host"),
            websocket.headers.get("x-forwarded-host"),
            websocket.headers.get("x-forwarded-proto"),
            str(websocket.url),
        )
        await websocket.close(code=1008)
        return
    if not has_admin_session(websocket):
        logger.warning("dashboard websocket rejected by missing or invalid admin session")
        await websocket.close(code=1008)
        return
    try:
        queue_upstream_id = _normalize_upstream_filter(websocket, websocket.query_params.get("queue_upstream_id"))
    except HTTPException:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    event_bus = getattr(websocket.app.state, "dashboard_events", None)
    last_version = event_bus.version if event_bus is not None else 0
    last_state = None
    receive_task = None
    event_task = None
    try:
        payload = dashboard_snapshot_payload(websocket, queue_upstream_id=queue_upstream_id, include_trends=False)
        last_state = _dashboard_snapshot_state(payload)
        await websocket.send_json(payload)
        receive_task = asyncio.create_task(websocket.receive_text())
        while True:
            event_task = (
                asyncio.create_task(event_bus.wait_for_change(last_version, DASHBOARD_WS_HEARTBEAT_SECONDS))
                if event_bus is not None
                else asyncio.create_task(asyncio.sleep(DASHBOARD_WS_HEARTBEAT_SECONDS, result=last_version))
            )
            done, pending = await asyncio.wait(
                [receive_task, event_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            if receive_task in done:
                receive_task.result()
                receive_task = asyncio.create_task(websocket.receive_text())
                if event_task not in done:
                    event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)
                    continue

            if event_task in done:
                next_version = event_task.result()
                if next_version != last_version:
                    last_version = next_version
                    payload = dashboard_snapshot_payload(websocket, queue_upstream_id=queue_upstream_id, include_trends=False)
                    current_state = _dashboard_snapshot_state(payload)
                    if current_state != last_state:
                        await websocket.send_json(payload)
                        last_state = current_state
                    continue

            await websocket.send_json(
                {
                    "type": "dashboard.heartbeat",
                    "server_time": datetime.now(DISPLAY_TIMEZONE).isoformat(),
                    "version": last_version,
                }
            )
    except WebSocketDisconnect:
        return
    finally:
        if receive_task is not None and not receive_task.done():
            receive_task.cancel()
            await asyncio.gather(receive_task, return_exceptions=True)
        if event_task is not None and not event_task.done():
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)


@web_router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    db: Database = request.app.state.db
    snapshot = dashboard_snapshot_payload(request, include_trends=False)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active": "dashboard",
            "stats": snapshot["stats"],
            "queue": snapshot["queue"],
            "upstream_weights": snapshot["upstream_weights"],
            "request_trends": _request_trend_stats(db),
            "upstream_choices": _upstream_choices(request),
        },
    )


def dashboard_snapshot_payload(
    request: Request | WebSocket,
    queue_upstream_id: str | None = None,
    trend_upstream_id: str | None = None,
    include_trends: bool = False,
):
    db: Database = request.app.state.db
    return {
        "type": "dashboard.snapshot",
        "server_time": datetime.now(DISPLAY_TIMEZONE).isoformat(),
        "stats": _dashboard_stats(request),
        "queue": queue_status_payload(request, upstream_id=queue_upstream_id),
        "upstream_weights": _upstream_weights_payload(request),
        "request_trends": _request_trend_stats(db, upstream_id=trend_upstream_id) if include_trends else None,
    }


def _dashboard_stats(request: Request | WebSocket) -> dict:
    db: Database = request.app.state.db
    today_start, today_end = local_day_range(datetime.now(DISPLAY_TIMEZONE))
    total_users = db.query_one("SELECT COUNT(*) AS c FROM users")["c"]
    today_requests = db.query_one(
        """
        SELECT COUNT(DISTINCT request_id) AS c
        FROM usage_logs
        WHERE datetime(created_at) >= datetime(?)
          AND datetime(created_at) < datetime(?)
        """,
        (to_utc_iso(today_start), to_utc_iso(today_end)),
    )["c"]
    total_anlas = db.query_one(
        "SELECT COALESCE(SUM(final_anlas_cost), 0) AS c FROM usage_logs WHERE status = 'success'"
    )["c"]
    return {
        "total_users": total_users,
        "today_requests": today_requests,
        "total_anlas": total_anlas,
        "queue_size": request.app.state.proxy_queue.qsize(),
    }


def _upstream_weights_payload(request: Request | WebSocket) -> dict:
    get_weights = getattr(request.app.state.proxy_queue, "get_weights", None)
    if callable(get_weights):
        return get_weights()
    return {"strategy": "unknown", "upstreams": []}


def queue_status_payload(request: Request | WebSocket, upstream_id: str | None = None):
    db: Database = request.app.state.db
    snapshot = request.app.state.proxy_queue.snapshot()
    if upstream_id:
        snapshot = _filter_queue_snapshot(snapshot, upstream_id)
    request_ids = [
        item["request_id"]
        for item in (snapshot.get("running_items") or ([snapshot["running"]] if snapshot["running"] else [])) + snapshot["queued"]
    ]
    log_details = _queue_log_details(db, request_ids)
    if snapshot["running"]:
        snapshot["running"] = _merge_queue_log_details(snapshot["running"], log_details)
    if snapshot.get("running_items"):
        snapshot["running_items"] = [_merge_queue_log_details(item, log_details) for item in snapshot["running_items"]]
    snapshot["queued"] = [_merge_queue_log_details(item, log_details) for item in snapshot["queued"]]
    return snapshot


def _stable_queue_state(queue: dict) -> dict:
    snapshot = dict(queue)
    if snapshot.get("running"):
        snapshot["running"] = _stable_queue_item(snapshot["running"])
    if snapshot.get("running_items"):
        snapshot["running_items"] = [_stable_queue_item(item) for item in snapshot["running_items"]]
    snapshot["queued"] = [_stable_queue_item(item) for item in snapshot.get("queued", [])]
    return snapshot


def _dashboard_snapshot_state(payload: dict) -> dict:
    return {
        "stats": payload["stats"],
        "queue": _stable_queue_state(payload["queue"]),
        "upstream_weights": payload["upstream_weights"],
        "request_trends": payload["request_trends"],
    }


def _stable_queue_item(item: dict) -> dict:
    stable = dict(item)
    stable.pop("queued_seconds", None)
    stable.pop("running_seconds", None)
    return stable


def _is_same_origin_websocket(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return False
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    expected_scheme = _expected_origin_scheme(websocket)
    expected_hostname, expected_port = _expected_origin_host(websocket)
    if not expected_hostname:
        return False
    if parsed.hostname != expected_hostname:
        return False
    if expected_port is None:
        if parsed.port is not None:
            return False
    elif _origin_port(parsed) != expected_port:
        return False
    return expected_scheme is None or parsed.scheme == expected_scheme


def _expected_origin_scheme(websocket: WebSocket) -> str | None:
    forwarded_proto = websocket.headers.get("x-forwarded-proto")
    if forwarded_proto:
        proto = forwarded_proto.split(",", 1)[0].strip().lower()
        if proto in {"http", "https"}:
            return proto
    if websocket.headers.get("host"):
        return None
    return "https" if websocket.url.scheme == "wss" else "http"


def _expected_origin_host(websocket: WebSocket) -> tuple[str | None, int | None]:
    forwarded_host = websocket.headers.get("x-forwarded-host")
    raw_host = (forwarded_host.split(",", 1)[0].strip() if forwarded_host else "") or websocket.headers.get("host")
    if raw_host:
        expected_scheme = _expected_origin_scheme(websocket)
        scheme = expected_scheme or "http"
        parsed = urlparse(f"{scheme}://{raw_host}")
        if parsed.hostname:
            return parsed.hostname, parsed.port if expected_scheme is None else _origin_port(parsed)
    return websocket.url.hostname, _url_port(websocket.url)


def _origin_port(parsed) -> int:
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def _url_port(url) -> int:
    if url.port is not None:
        return url.port
    return 443 if url.scheme == "wss" else 80


def _request_trend_stats(db: Database, upstream_id: str | None = None) -> dict:
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
                   COUNT(DISTINCT request_id) AS requests,
                   SUM(CASE WHEN lower(status) = 'failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN lower(status) = 'rejected' THEN 1 ELSE 0 END) AS rejected,
                   SUM(CASE WHEN lower(status) = 'success' AND is_retry_success = 1 THEN 1 ELSE 0 END) AS retry_success
            FROM usage_logs
            WHERE datetime(created_at) >= datetime(?)
              AND datetime(created_at) < datetime(?)
              AND (? IS NULL OR upstream_id = ?)
            GROUP BY bucket
            """,
            (to_utc_iso(today_start), to_utc_iso(today_end), upstream_id, upstream_id),
        ),
    )
    _fill_trend_range_from_rows(
        ranges["week"],
        _date_bucket_rows(db, week_start, week_end, upstream_id=upstream_id),
        _date_index_map(week_start, 7),
    )
    _fill_trend_range_from_rows(
        ranges["month"],
        _date_bucket_rows(db, month_start, month_end, upstream_id=upstream_id),
        _date_index_map(month_start, (month_end - month_start).days),
    )

    return ranges


def _date_bucket_rows(db: Database, start: datetime, end: datetime, upstream_id: str | None = None) -> list:
    return db.query_all(
        """
        SELECT date(datetime(created_at, '+8 hours')) AS bucket,
               COUNT(DISTINCT request_id) AS requests,
               SUM(CASE WHEN lower(status) = 'failed' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN lower(status) = 'rejected' THEN 1 ELSE 0 END) AS rejected,
               SUM(CASE WHEN lower(status) = 'success' AND is_retry_success = 1 THEN 1 ELSE 0 END) AS retry_success
        FROM usage_logs
        WHERE datetime(created_at) >= datetime(?)
          AND datetime(created_at) < datetime(?)
          AND (? IS NULL OR upstream_id = ?)
        GROUP BY bucket
        """,
        (to_utc_iso(start), to_utc_iso(end), upstream_id, upstream_id),
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
        retry_success = int(row["retry_success"] or 0)
        trend_range["series"]["requests"][index] = requests
        trend_range["series"]["failed"][index] = failed
        trend_range["series"]["rejected"][index] = rejected
        trend_range["series"]["retry_success"][index] = retry_success
        trend_range["totals"]["requests"] += requests
        trend_range["totals"]["failed"] += failed
        trend_range["totals"]["rejected"] += rejected
        trend_range["totals"]["retry_success"] += retry_success


def _empty_trend_range(labels: list[str], bucket_count: int) -> dict:
    return {
        "labels": labels,
        "series": {
            "requests": [0 for _ in range(bucket_count)],
            "failed": [0 for _ in range(bucket_count)],
            "rejected": [0 for _ in range(bucket_count)],
            "retry_success": [0 for _ in range(bucket_count)],
        },
        "totals": {"requests": 0, "failed": 0, "rejected": 0, "retry_success": 0},
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


def _upstream_choices(request: Request) -> list[str]:
    clients = getattr(request.app.state, "upstream_clients", None)
    if isinstance(clients, dict) and clients:
        return list(clients.keys())
    return ["default"]


def _normalize_upstream_filter(request: Request | WebSocket, upstream_id: str | None) -> str | None:
    normalized = (upstream_id or "").strip()
    if not normalized:
        return None
    if normalized not in set(_upstream_choices(request)):
        raise HTTPException(status_code=400, detail={"message": f"Unknown upstream id: {normalized}"})
    return normalized


def _filter_queue_snapshot(snapshot: dict, upstream_id: str) -> dict:
    filtered = dict(snapshot)
    running_items = [
        item
        for item in (snapshot.get("running_items") or ([snapshot["running"]] if snapshot.get("running") else []))
        if item.get("upstream_id") == upstream_id
    ]
    queued = [
        dict(item, position=index)
        for index, item in enumerate(
            [item for item in snapshot.get("queued", []) if item.get("upstream_id") == upstream_id],
            start=1,
        )
    ]
    filtered["running_items"] = running_items
    filtered["running"] = running_items[0] if len(running_items) == 1 else None
    filtered["queued"] = queued
    filtered["queue_size"] = len(queued)
    if "dispatch_queue_size" in filtered:
        filtered["dispatch_queue_size"] = 0
    if "upstreams" in filtered:
        filtered["upstreams"] = [item for item in filtered["upstreams"] if item.get("id") == upstream_id]
    return filtered
