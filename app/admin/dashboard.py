from __future__ import annotations

import asyncio
import base64
import io
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from novelai_python._exceptions import APIError
from novelai_python.sdk.ai._enum import Model, Sampler
from novelai_python.sdk.ai.generate_image import GenerateImageInfer

from ..dashboard_stats import ALL_UPSTREAMS, hour_bucket
from ..database import Database
from ..logging_utils import logger
from ..queue_manager import NoAvailableUpstream, QueueClosed, QueueFull, UpstreamExecutionTimeout
from ..templating import templates
from .auth import has_admin_session, require_admin_or_session, require_admin_page_session
from .common import (
    DISPLAY_TIMEZONE,
    add_month,
    local_day_range,
    row_to_dict,
    upstream_choices,
)


api_router = APIRouter(prefix="/admin/api")
web_router = APIRouter(prefix="/admin")
DASHBOARD_WS_HEARTBEAT_SECONDS = 30.0
DASHBOARD_QUEUE_DISPLAY_LIMIT = 80
ADMIN_UPSTREAM_TEST_PAYLOAD = {
    "input": "A simple red apple on a white plate.",
    "model": "nai-diffusion-3",
    "action": "generate",
    "parameters": {
        "width": 512,
        "height": 768,
        "scale": 5.0,
        "sampler": "k_euler_ancestral",
        "steps": 1,
        "n_samples": 1,
        "ucPreset": 0,
        "qualityToggle": False,
        "sm": False,
        "sm_dyn": False,
    },
}


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


@api_router.post("/upstreams/{upstream_id}/test", dependencies=[Depends(require_admin_or_session)])
async def test_upstream(request: Request, upstream_id: str):
    started_at = time.monotonic()
    try:
        normalized_upstream_id = _normalize_upstream_filter(request, upstream_id)
    except HTTPException as exc:
        return _upstream_test_failure_response(
            status_code=exc.status_code,
            upstream_id=upstream_id,
            started_at=started_at,
            error_code="unknown_upstream",
            error_type="UnknownUpstream",
            message=_http_exception_message(exc),
        )
    if normalized_upstream_id is None:
        return _upstream_test_failure_response(
            status_code=400,
            upstream_id=upstream_id,
            started_at=started_at,
            error_code="unknown_upstream",
            error_type="UnknownUpstream",
            message="Unknown upstream id",
        )

    proxy_queue = request.app.state.proxy_queue
    try:
        payload = await proxy_queue.submit_upstream_probe(
            upstream_id=normalized_upstream_id,
            request_id=f"admin-probe-{uuid.uuid4().hex}",
            logging_config=request.app.state.config.logging,
            handler=lambda upstream: upstream.generate_image_payload_zip(_admin_upstream_test_payload()),
        )
    except QueueFull:
        return _upstream_test_failure_response(
            status_code=503,
            upstream_id=normalized_upstream_id,
            started_at=started_at,
            error_code="queue_full",
            error_type="QueueFull",
            message="Queue full, please retry later",
        )
    except QueueClosed:
        return _upstream_test_failure_response(
            status_code=503,
            upstream_id=normalized_upstream_id,
            started_at=started_at,
            error_code="server_shutting_down",
            error_type="QueueClosed",
            message="Server is shutting down, please retry later",
        )
    except NoAvailableUpstream as exc:
        return _upstream_test_failure_response(
            status_code=400,
            upstream_id=normalized_upstream_id,
            started_at=started_at,
            error_code="unknown_upstream",
            error_type=exc.__class__.__name__,
            message=str(exc),
        )
    except UpstreamExecutionTimeout as exc:
        return _upstream_test_failure_response(
            status_code=504,
            upstream_id=normalized_upstream_id,
            started_at=started_at,
            error_code="upstream_timeout",
            error_type=exc.__class__.__name__,
            message=str(exc),
        )
    except APIError as exc:
        return _upstream_test_failure_response(
            status_code=_api_error_status_code(exc),
            upstream_id=normalized_upstream_id,
            started_at=started_at,
            error_code=str(exc.code or "upstream_error"),
            error_type=_api_error_type(exc),
            message=exc.message or str(exc),
        )
    except Exception as exc:
        logger.exception("admin upstream test failed upstream_id=%s", normalized_upstream_id)
        return _upstream_test_failure_response(
            status_code=502,
            upstream_id=normalized_upstream_id,
            started_at=started_at,
            error_code=exc.__class__.__name__,
            error_type=exc.__class__.__name__,
            message=str(exc) or "Upstream test failed",
        )

    image_count, preview_image = _zip_image_preview(payload)
    if preview_image is None:
        return _upstream_test_failure_response(
            status_code=502,
            upstream_id=normalized_upstream_id,
            started_at=started_at,
            error_code="invalid_upstream_response",
            error_type="InvalidUpstreamResponse",
            message="Upstream returned a zip without a valid image",
        )

    return {
        "ok": True,
        "upstream_id": normalized_upstream_id,
        "elapsed_ms": _elapsed_ms(started_at),
        "zip_bytes": len(payload),
        "image_count": image_count,
        "preview_image": preview_image,
        "message": "Upstream test succeeded",
    }


# websocket 路由与页面共用本 router，会话依赖挂在各 HTML 路由上而非 router 级。
@web_router.get("", response_class=HTMLResponse, dependencies=[Depends(require_admin_page_session)])
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


@web_router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_admin_page_session)])
async def dashboard(request: Request):
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
            "upstream_choices": upstream_choices(request),
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
        "queue": queue_status_payload(
            request,
            upstream_id=queue_upstream_id,
            queued_limit=DASHBOARD_QUEUE_DISPLAY_LIMIT,
        ),
        "upstream_weights": _upstream_weights_payload(request),
        "request_trends": _request_trend_stats(db, upstream_id=trend_upstream_id) if include_trends else None,
    }


def _dashboard_stats(request: Request | WebSocket) -> dict:
    db: Database = request.app.state.db
    today_start, today_end = local_day_range(datetime.now(DISPLAY_TIMEZONE))
    total_users = db.query_one("SELECT COUNT(*) AS c FROM users WHERE deleted_at IS NULL")["c"]
    today_requests = _request_count_for_range(db, today_start, today_end)
    total_anlas = db.query_one(
        "SELECT COALESCE(SUM(anlas_cost), 0) AS c FROM dashboard_hourly_stats WHERE upstream_id = ?",
        (ALL_UPSTREAMS,),
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


def _admin_upstream_test_payload() -> dict:
    params = ADMIN_UPSTREAM_TEST_PAYLOAD["parameters"]
    req = GenerateImageInfer.build_generate(
        ADMIN_UPSTREAM_TEST_PAYLOAD["input"],
        model=Model(ADMIN_UPSTREAM_TEST_PAYLOAD["model"]),
        width=params["width"],
        height=params["height"],
        sampler=Sampler(params["sampler"]),
        steps=params["steps"],
        ucPreset=params["ucPreset"],
        qualityToggle=params["qualityToggle"],
    )
    req.parameters.scale = params["scale"]
    req.parameters.n_samples = params["n_samples"]
    return req.model_dump(mode="json", exclude_none=True)


def _upstream_test_failure_response(
    *,
    status_code: int,
    upstream_id: str,
    started_at: float,
    error_code: str,
    error_type: str,
    message: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "upstream_id": upstream_id,
            "elapsed_ms": _elapsed_ms(started_at),
            "error_code": error_code,
            "error_type": error_type,
            "message": message,
        },
    )


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((time.monotonic() - started_at) * 1000))


def _zip_image_preview(payload: bytes) -> tuple[int, dict[str, object] | None]:
    image_count = 0
    preview_image = None
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for item in archive.infolist():
                if item.is_dir() or item.file_size <= 0:
                    continue
                content_type = _image_content_type(item.filename)
                if content_type is None:
                    continue
                image_count += 1
                if preview_image is None:
                    image_bytes = archive.read(item)
                    preview_image = {
                        "filename": item.filename,
                        "content_type": content_type,
                        "bytes": len(image_bytes),
                        "data_url": f"data:{content_type};base64,{base64.b64encode(image_bytes).decode('ascii')}",
                    }
    except zipfile.BadZipFile:
        return 0, None
    return image_count, preview_image


def _image_content_type(filename: str) -> str | None:
    lower = filename.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".webp"):
        return "image/webp"
    return None


def _api_error_status_code(exc: APIError) -> int:
    return int(exc.code) if str(exc.code or "").isdigit() else 502


def _api_error_type(exc: APIError) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error_type = response.get("type") or response.get("errorType") or response.get("name")
        if error_type:
            return str(error_type)
    return exc.__class__.__name__


def _http_exception_message(exc: HTTPException) -> str:
    if isinstance(exc.detail, dict):
        return str(exc.detail.get("message") or exc.detail)
    return str(exc.detail)


def queue_status_payload(request: Request | WebSocket, upstream_id: str | None = None, queued_limit: int | None = None):
    db: Database = request.app.state.db
    snapshot = request.app.state.proxy_queue.snapshot()
    if upstream_id:
        snapshot = _filter_queue_snapshot(snapshot, upstream_id)
    if queued_limit is not None:
        snapshot = _limit_queue_snapshot(snapshot, queued_limit)
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


def _limit_queue_snapshot(snapshot: dict, queued_limit: int) -> dict:
    limit = max(0, int(queued_limit))
    limited = dict(snapshot)
    queued = list(snapshot.get("queued", []))
    total = len(queued)
    limited["queued"] = queued[:limit]
    limited["queued_total"] = total
    limited["queued_hidden"] = max(0, total - len(limited["queued"]))
    limited["queued_display_limit"] = limit
    if "upstreams" in limited:
        limited["upstreams"] = [_limit_nested_upstream_queue(item) for item in limited["upstreams"]]
    return limited


def _limit_nested_upstream_queue(upstream: dict) -> dict:
    limited = dict(upstream)
    queued = list(upstream.get("queued", []))
    limited["queued"] = []
    limited["queued_total"] = len(queued)
    limited["queued_hidden"] = len(queued)
    limited["queued_display_limit"] = 0
    return limited


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
        _hour_bucket_rows(db, today_start, today_end, upstream_id=upstream_id),
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
    ranges["today"]["totals"]["requests"] = _request_count_for_range(db, today_start, today_end, upstream_id)
    ranges["week"]["totals"]["requests"] = _request_count_for_range(db, week_start, week_end, upstream_id)
    ranges["month"]["totals"]["requests"] = _request_count_for_range(db, month_start, month_end, upstream_id)

    return ranges


def _request_count_for_range(db: Database, start: datetime, end: datetime, upstream_id: str | None = None) -> int:
    row = db.query_one(
        """
        SELECT COUNT(DISTINCT request_id) AS c
        FROM dashboard_hourly_request_refs
        WHERE bucket_hour >= ?
          AND bucket_hour < ?
          AND upstream_id = ?
        """,
        (hour_bucket(start), hour_bucket(end), _stats_upstream_id(upstream_id)),
    )
    return int(row["c"] or 0)


def _hour_bucket_rows(db: Database, start: datetime, end: datetime, upstream_id: str | None = None) -> list:
    start_bucket = hour_bucket(start)
    end_bucket = hour_bucket(end)
    stats_upstream_id = _stats_upstream_id(upstream_id)
    return db.query_all(
        """
        WITH request_rows AS (
            SELECT CAST(substr(bucket_hour, 12, 2) AS INTEGER) AS bucket,
                   COUNT(DISTINCT request_id) AS requests
            FROM dashboard_hourly_request_refs
            WHERE bucket_hour >= ?
              AND bucket_hour < ?
              AND upstream_id = ?
            GROUP BY bucket
        ),
        status_rows AS (
            SELECT CAST(substr(bucket_hour, 12, 2) AS INTEGER) AS bucket,
                   SUM(failed_count) AS failed,
                   SUM(rejected_count) AS rejected,
                   SUM(retry_success_count) AS retry_success
            FROM dashboard_hourly_stats
            WHERE bucket_hour >= ?
              AND bucket_hour < ?
              AND upstream_id = ?
            GROUP BY bucket
        ),
        buckets AS (
            SELECT bucket FROM request_rows
            UNION
            SELECT bucket FROM status_rows
        )
        SELECT buckets.bucket AS bucket,
               COALESCE(request_rows.requests, 0) AS requests,
               COALESCE(status_rows.failed, 0) AS failed,
               COALESCE(status_rows.rejected, 0) AS rejected,
               COALESCE(status_rows.retry_success, 0) AS retry_success
        FROM buckets
        LEFT JOIN request_rows ON request_rows.bucket = buckets.bucket
        LEFT JOIN status_rows ON status_rows.bucket = buckets.bucket
        ORDER BY buckets.bucket
        """,
        (
            start_bucket,
            end_bucket,
            stats_upstream_id,
            start_bucket,
            end_bucket,
            stats_upstream_id,
        ),
    )
def _date_bucket_rows(db: Database, start: datetime, end: datetime, upstream_id: str | None = None) -> list:
    start_bucket = hour_bucket(start)
    end_bucket = hour_bucket(end)
    stats_upstream_id = _stats_upstream_id(upstream_id)
    return db.query_all(
        """
        WITH request_rows AS (
            SELECT substr(bucket_hour, 1, 10) AS bucket,
                   COUNT(DISTINCT request_id) AS requests
            FROM dashboard_hourly_request_refs
            WHERE bucket_hour >= ?
              AND bucket_hour < ?
              AND upstream_id = ?
            GROUP BY bucket
        ),
        status_rows AS (
            SELECT substr(bucket_hour, 1, 10) AS bucket,
                   SUM(failed_count) AS failed,
                   SUM(rejected_count) AS rejected,
                   SUM(retry_success_count) AS retry_success
            FROM dashboard_hourly_stats
            WHERE bucket_hour >= ?
              AND bucket_hour < ?
              AND upstream_id = ?
            GROUP BY bucket
        ),
        buckets AS (
            SELECT bucket FROM request_rows
            UNION
            SELECT bucket FROM status_rows
        )
        SELECT buckets.bucket AS bucket,
               COALESCE(request_rows.requests, 0) AS requests,
               COALESCE(status_rows.failed, 0) AS failed,
               COALESCE(status_rows.rejected, 0) AS rejected,
               COALESCE(status_rows.retry_success, 0) AS retry_success
        FROM buckets
        LEFT JOIN request_rows ON request_rows.bucket = buckets.bucket
        LEFT JOIN status_rows ON status_rows.bucket = buckets.bucket
        ORDER BY buckets.bucket
        """,
        (
            start_bucket,
            end_bucket,
            stats_upstream_id,
            start_bucket,
            end_bucket,
            stats_upstream_id,
        ),
    )


def _stats_upstream_id(upstream_id: str | None) -> str:
    return upstream_id or ALL_UPSTREAMS


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


def _normalize_upstream_filter(request: Request | WebSocket, upstream_id: str | None) -> str | None:
    normalized = (upstream_id or "").strip()
    if not normalized:
        return None
    if normalized not in set(upstream_choices(request)):
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
