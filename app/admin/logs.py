from __future__ import annotations

import base64
import io
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from novelai_python._exceptions import APIError

from ..logging_utils import mark_request_total_duration
from ..novelai_endpoints import ENCODE_VIBE_ENDPOINT, replay_endpoint_for
from ..payload_archive import PayloadArchiveError, PayloadArchiveService, PayloadNotFoundError
from ..queue_manager import NoAvailableUpstream, QueueFull, UpstreamExecutionTimeout
from ..queue_tiers import TIER_REPLAY
from ..templating import templates
from ..timezones import DISPLAY_TIMEZONE
from ..usage_logs import USAGE_LOG_STATUSES, UsageLogCreate, UsageLogRepository
from .auth import require_admin_or_session, require_admin_page_session
from .common import json_or_none, optional_query_int, row_to_dict, usage_log_to_dict


api_router = APIRouter(prefix="/admin/api")
web_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_page_session)])
REPLAY_IMAGE_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
LOG_STATUS_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "success": "成功",
    "failed": "失败",
    "rejected": "已拒绝",
}


@dataclass(frozen=True)
class LogFilters:
    user_id: int | None
    created_from: str
    created_to: str
    created_from_utc: str | None
    created_to_utc: str | None
    action: str | None
    status: str | None


@api_router.get("/logs", dependencies=[Depends(require_admin_or_session)])
async def logs(
    request: Request,
    user_id: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    action: str | None = None,
    status: str | None = None,
    limit: int = 100,
    before_id: int | None = None,
):
    usage_logs: UsageLogRepository = request.app.state.usage_logs
    filters = _parse_log_filters(
        user_id=user_id,
        created_from=created_from,
        created_to=created_to,
        action=action,
        status=status,
    )
    limit = max(1, min(limit, 500))
    before_id = before_id if before_id is not None and before_id > 0 else None
    rows = usage_logs.list_logs(
        user_id=filters.user_id,
        created_from=filters.created_from_utc,
        created_to=filters.created_to_utc,
        action=filters.action,
        status=filters.status,
        limit=limit,
        before_id=before_id,
    )
    page_rows = rows[:limit]
    return {
        "logs": [usage_log_to_dict(row) for row in page_rows],
        "limit": limit,
        "before_id": before_id,
        "next_before_id": int(page_rows[-1]["id"]) if page_rows else None,
        "has_more": len(rows) > limit,
    }


@api_router.post("/logs/by-id/{log_id}/replay", dependencies=[Depends(require_admin_or_session)])
async def replay_log_by_id(log_id: int, request: Request):
    usage_logs: UsageLogRepository = request.app.state.usage_logs
    source = usage_logs.get_by_id(log_id)
    if source is None:
        raise HTTPException(status_code=404, detail={"message": "Log not found"})
    return await _replay_log_source(source, request)


@api_router.get("/logs/by-id/{log_id}/payload", dependencies=[Depends(require_admin_or_session)])
async def log_payload_by_id(log_id: int, request: Request):
    payload_archive: PayloadArchiveService = request.app.state.payload_archive_service
    usage_logs: UsageLogRepository = request.app.state.usage_logs
    source = usage_logs.get_by_id(log_id)
    if source is None:
        raise HTTPException(status_code=404, detail={"message": "Log not found"})
    try:
        payload_text = payload_archive.get_payload_text(log_id)
    except PayloadNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"message": str(exc)}) from exc
    except PayloadArchiveError as exc:
        raise HTTPException(status_code=500, detail={"message": str(exc)}) from exc
    return {
        "ok": True,
        "log_id": log_id,
        "payload_archived": bool(source["payload_archived"]),
        "request_payload": json_or_none(payload_text),
        "request_payload_text": payload_text,
    }


@api_router.post("/logs/{request_id}/replay", dependencies=[Depends(require_admin_or_session)])
async def replay_log_request(request_id: str, request: Request):
    usage_logs: UsageLogRepository = request.app.state.usage_logs
    source = usage_logs.get_by_request_id(request_id)
    if source is None:
        raise HTTPException(status_code=404, detail={"message": "Log not found"})
    return await _replay_log_source(source, request)


async def _replay_log_source(source, request: Request):
    usage_logs: UsageLogRepository = request.app.state.usage_logs
    payload_archive: PayloadArchiveService = request.app.state.payload_archive_service
    try:
        request_payload = payload_archive.get_payload_dict(int(source["id"]))
    except PayloadNotFoundError as exc:
        raise HTTPException(status_code=400, detail={"message": "This log has no replayable request payload"}) from exc
    except PayloadArchiveError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": "This log has no replayable request payload"})

    endpoint = replay_endpoint_for(str(source["action"]), request_payload)
    if endpoint is None:
        raise HTTPException(status_code=400, detail={"message": "This log action is not replayable"})

    replay_request_id = uuid.uuid4().hex
    action = f"replay:{source['action']}"
    usage_logs.insert_queued(
        UsageLogCreate(
            request_id=replay_request_id,
            user_id=int(source["user_id"]),
            action=action,
            model=source["model"],
            width=source["width"],
            height=source["height"],
            steps=source["steps"],
            n_samples=source["n_samples"],
            estimated_anlas_cost=0,
            request_payload=request_payload,
        )
    )

    try:
        # 管理员重放用于排查历史请求，不再次预留用户额度。
        binary_payload = await request.app.state.proxy_queue.submit(
            request_id=replay_request_id,
            user_id=int(source["user_id"]),
            tier=TIER_REPLAY,
            action=action,
            logging_config=request.app.state.config.logging,
            estimated_cost=0,
            handler=lambda upstream: upstream.post_binary(endpoint, request_payload),
            process_zip_response=endpoint != ENCODE_VIBE_ENDPOINT,
            manage_quota=False,
            allowed_upstreams=[source["upstream_id"]] if source["upstream_id"] else None,
        )
    except QueueFull:
        usage_logs.mark_rejected(
            replay_request_id,
            error_code="queue_full",
            error_message="Queue full, please retry later",
            log_level="ERROR",
        )
        raise HTTPException(status_code=503, detail={"message": "Queue full, please retry later"}) from None
    except NoAvailableUpstream as exc:
        usage_logs.mark_rejected(
            replay_request_id,
            error_code="no_available_upstream",
            error_message=str(exc),
            log_level="ERROR",
        )
        raise HTTPException(status_code=503, detail={"message": "No enabled upstream is available for this replay"}) from exc
    except APIError as exc:
        status_code = int(exc.code) if str(exc.code or "").isdigit() else 502
        raise HTTPException(status_code=status_code, detail={"message": exc.message}) from exc
    except UpstreamExecutionTimeout as exc:
        raise HTTPException(status_code=504, detail={"message": str(exc)}) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc)}) from exc
    finally:
        mark_request_total_duration(request, replay_request_id)

    return {
        "ok": True,
        "source_log_id": int(source["id"]),
        "source_request_id": source["request_id"],
        "source_attempt_number": int(source["attempt_number"]),
        "replay_request_id": replay_request_id,
        "images": _zip_images_to_data_urls(binary_payload),
    }


@web_router.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    user_id: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    action: str | None = None,
    status: str | None = None,
    limit: int = 100,
):
    filters = _parse_log_filters(
        user_id=user_id,
        created_from=created_from,
        created_to=created_to,
        action=action,
        status=status,
    )
    data = await logs(
        request,
        user_id=user_id,
        created_from=created_from,
        created_to=created_to,
        action=action,
        status=status,
        limit=limit,
    )
    usage_logs: UsageLogRepository = request.app.state.usage_logs
    action_options = usage_logs.list_actions()
    if filters.action is not None and filters.action not in action_options:
        action_options.append(filters.action)
        action_options.sort()
    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "active": "logs",
            "logs": data["logs"],
            "selected_user": _selected_user(request, filters.user_id),
            "selected_user_id": filters.user_id,
            "created_from": filters.created_from,
            "created_to": filters.created_to,
            "action_options": action_options,
            "selected_action": filters.action,
            "status_options": [{"value": value, "label": LOG_STATUS_LABELS[value]} for value in USAGE_LOG_STATUSES],
            "selected_status": filters.status,
            "limit": data["limit"],
            "has_more": data["has_more"],
            "next_before_id": data["next_before_id"],
        },
    )


def _parse_log_filters(
    *,
    user_id: str | None,
    created_from: str | None,
    created_to: str | None,
    action: str | None,
    status: str | None,
) -> LogFilters:
    parsed_from, created_from_utc = _parse_datetime_local_filter(created_from, "created_from")
    parsed_to, created_to_utc = _parse_datetime_local_filter(created_to, "created_to")
    if created_from_utc is not None and created_to_utc is not None and created_from_utc > created_to_utc:
        raise HTTPException(status_code=400, detail={"message": "created_from must be earlier than created_to"})
    return LogFilters(
        user_id=optional_query_int(user_id),
        created_from=parsed_from,
        created_to=parsed_to,
        created_from_utc=created_from_utc,
        created_to_utc=created_to_utc,
        action=_optional_query_text(action),
        status=_parse_status_filter(status),
    )


def _optional_query_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _parse_status_filter(value: str | None) -> str | None:
    status = _optional_query_text(value)
    if status is None:
        return None
    if status not in USAGE_LOG_STATUSES:
        raise HTTPException(status_code=400, detail={"message": "Invalid status filter"})
    return status


def _parse_datetime_local_filter(value: str | None, field_name: str) -> tuple[str, str | None]:
    raw = _optional_query_text(value)
    if raw is None:
        return "", None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": f"Invalid {field_name} filter"}) from exc
    if parsed.tzinfo is None:
        local_time = parsed.replace(tzinfo=DISPLAY_TIMEZONE)
    else:
        local_time = parsed.astimezone(DISPLAY_TIMEZONE)
    return local_time.strftime("%Y-%m-%dT%H:%M"), local_time.astimezone(timezone.utc).isoformat()


def _selected_user(request: Request, user_id: int | None) -> dict | None:
    if user_id is None:
        return None
    row = request.app.state.db.query_one("SELECT id, name FROM users WHERE id = ?", (user_id,))
    return row_to_dict(row) if row is not None else None


def _zip_images_to_data_urls(zip_payload: bytes) -> list[dict[str, str | int]]:
    images = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_payload)) as zip_file:
            for member in zip_file.infolist():
                if member.is_dir():
                    continue
                suffix = Path(member.filename).suffix.lower()
                content_type = REPLAY_IMAGE_CONTENT_TYPES.get(suffix)
                if content_type is None:
                    continue
                data = zip_file.read(member)
                if not data:
                    continue
                images.append(
                    {
                        "filename": member.filename,
                        "content_type": content_type,
                        "bytes": len(data),
                        "data_url": f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}",
                    }
                )
    except zipfile.BadZipFile:
        return []
    return images
