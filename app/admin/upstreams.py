from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..templating import templates
from ..upstreams import upstream_to_public_dict
from .auth import require_admin_or_session, require_admin_page_session
from .common import format_display_time


api_router = APIRouter(prefix="/admin/api")
web_router = APIRouter(prefix="/admin")


class UpstreamCreateInput(BaseModel):
    id: str
    api_key: str
    enabled: bool = True


class UpstreamUpdateInput(BaseModel):
    api_key: str | None = None
    enabled: bool | None = None


class NovelAISettingsUpdateInput(BaseModel):
    account_tier: int | None = Field(default=None, ge=0, le=3)
    upscale_anlas_cost: int | None = Field(default=None, ge=0)


@api_router.get("/upstreams", dependencies=[Depends(require_admin_or_session)])
async def list_upstreams(request: Request):
    runtime = request.app.state.upstream_runtime
    repo = runtime.repository
    return {
        "upstreams": [_upstream_to_admin_dict(row) for row in repo.list(include_disabled=True)],
        "settings": _settings_to_dict(repo.get_settings()),
    }


@api_router.post("/upstreams", dependencies=[Depends(require_admin_or_session)])
async def create_upstream(request: Request, payload: UpstreamCreateInput):
    runtime = request.app.state.upstream_runtime
    record = runtime.repository.create(
        upstream_id=payload.id,
        api_key=payload.api_key,
        enabled=payload.enabled,
    )
    runtime.reload_upstream(record.id)
    return {"upstream": _upstream_to_admin_dict(record)}


@api_router.patch("/upstreams/{upstream_id:path}", dependencies=[Depends(require_admin_or_session)])
async def update_upstream(request: Request, upstream_id: str, payload: UpstreamUpdateInput):
    runtime = request.app.state.upstream_runtime
    record = runtime.repository.update(
        upstream_id,
        api_key=payload.api_key,
        enabled=payload.enabled,
    )
    runtime.reload_upstream(record.id)
    return {"upstream": _upstream_to_admin_dict(record)}


@api_router.delete("/upstreams/{upstream_id:path}", dependencies=[Depends(require_admin_or_session)])
async def delete_upstream(request: Request, upstream_id: str):
    runtime = request.app.state.upstream_runtime
    runtime.repository.delete(upstream_id)
    runtime.reload_upstream(upstream_id)
    return {"ok": True}


@api_router.patch("/novelai-settings", dependencies=[Depends(require_admin_or_session)])
async def update_novelai_settings(request: Request, payload: NovelAISettingsUpdateInput):
    runtime = request.app.state.upstream_runtime
    settings = runtime.repository.update_settings(
        account_tier=payload.account_tier,
        upscale_anlas_cost=payload.upscale_anlas_cost,
    )
    return {"settings": _settings_to_dict(settings)}


@web_router.get("/upstreams", response_class=HTMLResponse, dependencies=[Depends(require_admin_page_session)])
async def upstreams_page(request: Request):
    runtime = request.app.state.upstream_runtime
    repo = runtime.repository
    notification_repo = request.app.state.admin_notifications
    auto_disabled_upstream_ids = {
        str(notification.metadata.get("upstream_id"))
        for notification in notification_repo.pending()
        if notification.event_type == "upstream_auto_disabled" and notification.metadata.get("upstream_id")
    }
    return templates.TemplateResponse(
        request,
        "upstreams.html",
        {
            "active": "upstreams",
            "upstreams": [_upstream_to_admin_dict(row) for row in repo.list(include_disabled=True)],
            "settings": _settings_to_dict(repo.get_settings()),
            "auto_disabled_upstream_ids": auto_disabled_upstream_ids,
        },
    )


def _upstream_to_admin_dict(record) -> dict:
    data = upstream_to_public_dict(record)
    changed_at = data.get("updated_at") or data.get("created_at")
    data["changed_at_display"] = format_display_time(changed_at)
    return data


def _settings_to_dict(settings) -> dict:
    return {
        "account_tier": settings.account_tier,
        "upscale_anlas_cost": settings.upscale_anlas_cost,
        "created_at": settings.created_at,
        "updated_at": settings.updated_at,
    }
