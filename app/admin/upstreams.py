from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..templating import templates
from ..upstreams import upstream_to_public_dict
from .auth import require_admin_or_session, require_admin_page_session
from .common import format_display_time


api_router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin_or_session)])
web_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_page_session)])


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


@api_router.get("/upstreams")
async def list_upstreams(request: Request):
    runtime = request.app.state.upstream_runtime
    repo = runtime.repository
    records = repo.list(include_disabled=True)
    owner_map = _build_owner_map(request.app.state.db, records)
    return {
        "upstreams": [_upstream_to_admin_dict(row, owner_map=owner_map) for row in records],
        "settings": _settings_to_dict(repo.get_settings()),
    }


@api_router.post("/upstreams")
async def create_upstream(request: Request, payload: UpstreamCreateInput):
    runtime = request.app.state.upstream_runtime
    record = runtime.repository.create(
        upstream_id=payload.id,
        api_key=payload.api_key,
        enabled=payload.enabled,
    )
    runtime.reload_upstream(record.id)
    return {"upstream": _upstream_to_admin_dict(record, owner_map=_build_owner_map(request.app.state.db, [record]))}


@api_router.patch("/upstreams/{upstream_id:path}")
async def update_upstream(request: Request, upstream_id: str, payload: UpstreamUpdateInput):
    runtime = request.app.state.upstream_runtime
    record = runtime.repository.update(
        upstream_id,
        api_key=payload.api_key,
        enabled=payload.enabled,
    )
    runtime.reload_upstream(record.id)
    return {"upstream": _upstream_to_admin_dict(record, owner_map=_build_owner_map(request.app.state.db, [record]))}


@api_router.delete("/upstreams/{upstream_id:path}")
async def delete_upstream(request: Request, upstream_id: str):
    runtime = request.app.state.upstream_runtime
    runtime.repository.delete(upstream_id)
    runtime.reload_upstream(upstream_id)
    return {"ok": True}


@api_router.patch("/novelai-settings")
async def update_novelai_settings(request: Request, payload: NovelAISettingsUpdateInput):
    runtime = request.app.state.upstream_runtime
    settings = runtime.repository.update_settings(
        account_tier=payload.account_tier,
        upscale_anlas_cost=payload.upscale_anlas_cost,
    )
    return {"settings": _settings_to_dict(settings)}


@web_router.get("/upstreams", response_class=HTMLResponse)
async def upstreams_page(request: Request):
    runtime = request.app.state.upstream_runtime
    repo = runtime.repository
    notification_repo = request.app.state.admin_notifications
    auto_disabled_upstream_ids = notification_repo.pending_upstream_ids(
        "upstream_auto_disabled"
    )
    records = repo.list(include_disabled=True)
    owner_map = _build_owner_map(request.app.state.db, records)
    return templates.TemplateResponse(
        request,
        "upstreams.html",
        {
            "active": "upstreams",
            "upstreams": [_upstream_to_admin_dict(row, owner_map=owner_map) for row in records],
            "settings": _settings_to_dict(repo.get_settings()),
            "auto_disabled_upstream_ids": auto_disabled_upstream_ids,
        },
    )


def _build_owner_map(db, records: list) -> dict[int, dict]:
    """批量查询上传者信息，避免对每个上游做一次 N+1 查询。"""
    owner_ids = {record.owner_user_id for record in records if record.owner_user_id is not None}
    if not owner_ids:
        return {}
    placeholders = ",".join("?" for _ in owner_ids)
    rows = db.query_all(
        f"SELECT id, name, deleted_at FROM users WHERE id IN ({placeholders})",
        tuple(sorted(owner_ids)),
    )
    return {int(row["id"]): {"name": row["name"], "deleted_at": row["deleted_at"]} for row in rows}


def _upstream_to_admin_dict(record, *, owner_map: dict | None = None) -> dict:
    data = upstream_to_public_dict(record)
    changed_at = data.get("updated_at") or data.get("created_at")
    data["changed_at_display"] = format_display_time(changed_at)
    data["owner_user"] = None
    if record.owner_user_id is not None and owner_map:
        data["owner_user"] = owner_map.get(record.owner_user_id)
    return data


def _settings_to_dict(settings) -> dict:
    return {
        "account_tier": settings.account_tier,
        "upscale_anlas_cost": settings.upscale_anlas_cost,
        "created_at": settings.created_at,
        "updated_at": settings.updated_at,
    }
