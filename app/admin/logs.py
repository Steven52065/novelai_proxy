from __future__ import annotations

import base64
import io
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from novelai_python._exceptions import APIError

from ..queue_manager import QueueFull
from ..usage_logs import UsageLogCreate, UsageLogRepository
from .auth import has_admin_session, require_admin_or_session
from .common import json_or_none, optional_query_int, row_to_dict, templates, usage_log_to_dict


api_router = APIRouter(prefix="/admin/api")
web_router = APIRouter(prefix="/admin")
REPLAY_PRIORITY = -100
REPLAY_IMAGE_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


@api_router.get("/logs", dependencies=[Depends(require_admin_or_session)])
async def logs(request: Request, user_id: int | None = None, limit: int = 100, before_id: int | None = None):
    usage_logs: UsageLogRepository = request.app.state.usage_logs
    limit = max(1, min(limit, 500))
    before_id = before_id if before_id is not None and before_id > 0 else None
    rows = usage_logs.list_logs(user_id=user_id, limit=limit, before_id=before_id)
    page_rows = rows[:limit]
    return {
        "logs": [usage_log_to_dict(row) for row in page_rows],
        "limit": limit,
        "before_id": before_id,
        "next_before_id": int(page_rows[-1]["id"]) if page_rows else None,
        "has_more": len(rows) > limit,
    }


@api_router.post("/logs/{request_id}/replay", dependencies=[Depends(require_admin_or_session)])
async def replay_log_request(request_id: str, request: Request):
    usage_logs: UsageLogRepository = request.app.state.usage_logs
    source = usage_logs.get_by_request_id(request_id)
    if source is None:
        raise HTTPException(status_code=404, detail={"message": "Log not found"})

    request_payload = json_or_none(source["request_payload"])
    if not isinstance(request_payload, dict):
        raise HTTPException(status_code=400, detail={"message": "This log has no replayable request payload"})

    endpoint = _replay_endpoint(str(source["action"]), request_payload)
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
            tier="replay",
            action=action,
            logging_config=request.app.state.config.logging,
            estimated_cost=0,
            handler=lambda: request.app.state.upstream.post_binary(endpoint, request_payload),
            process_zip_response=endpoint != _encode_vibe_endpoint(),
            priority_override=REPLAY_PRIORITY,
            manage_quota=False,
        )
    except QueueFull:
        usage_logs.mark_rejected(
            replay_request_id,
            error_code="queue_full",
            error_message="Queue full, please retry later",
            log_level="ERROR",
        )
        raise HTTPException(status_code=503, detail={"message": "Queue full, please retry later"}) from None
    except APIError as exc:
        status_code = int(exc.code) if str(exc.code or "").isdigit() else 502
        raise HTTPException(status_code=status_code, detail={"message": exc.message}) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc)}) from exc

    return {
        "ok": True,
        "source_request_id": request_id,
        "replay_request_id": replay_request_id,
        "images": _zip_images_to_data_urls(binary_payload),
    }


@web_router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, user_id: str | None = None, limit: int = 100):
    if not has_admin_session(request):
        return RedirectResponse("/admin/login", status_code=303)
    selected_user_id = optional_query_int(user_id)
    data = await logs(request, user_id=selected_user_id, limit=limit)
    users = request.app.state.db.query_all("SELECT id, name FROM users ORDER BY name")
    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "active": "logs",
            "logs": data["logs"],
            "users": [row_to_dict(row) for row in users],
            "selected_user_id": selected_user_id,
            "limit": data["limit"],
            "has_more": data["has_more"],
            "next_before_id": data["next_before_id"],
        },
    )


def _replay_endpoint(action: str, payload: dict) -> str | None:
    if action == "upscale":
        return "https://api.novelai.net/ai/upscale"
    if action == "encode-vibe":
        return _encode_vibe_endpoint()
    if isinstance(payload.get("parameters"), dict) and isinstance(payload.get("model"), str):
        return "https://image.novelai.net/ai/generate-image"
    if isinstance(payload.get("req_type"), str):
        return "https://image.novelai.net/ai/augment-image"
    return None


def _encode_vibe_endpoint() -> str:
    return "https://image.novelai.net/ai/encode-vibe"


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
