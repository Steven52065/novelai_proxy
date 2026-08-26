from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..config import AppConfig, DiscordSelfServiceConfig, SelfServiceUpstreamsConfig
from ..database import Database
from ..deps import get_config, get_db, get_discord_self_service_config
from ..domain_errors import UpstreamConflict, UpstreamNotFound
from ..upstreams import (
    NovelAIUpstreamRecord,
    NovelAIUpstreamRepository,
    upstream_to_public_dict,
)
from .session import (
    current_self_service_user_id,
    ensure_self_service_account_active,
    require_discord_enabled,
)

if TYPE_CHECKING:
    from ..admin_notifications import AdminNotificationRepository

router = APIRouter()


class UpstreamCreateInput(BaseModel):
    label: str = ""
    api_key: str
    enabled: bool = True


class UpstreamUpdateInput(BaseModel):
    api_key: str | None = None
    enabled: bool | None = None


@router.get("/account/api/upstreams")
async def list_upstreams(
    request: Request,
    discord_config: DiscordSelfServiceConfig = Depends(get_discord_self_service_config),
    app_config: AppConfig = Depends(get_config),
    db: Database = Depends(get_db),
):
    require_discord_enabled(discord_config)
    _require_upstreams_enabled(app_config)
    user_id = _require_login(request, discord_config)
    ensure_self_service_account_active(db, user_id)
    repo = NovelAIUpstreamRepository(db)
    return {
        "upstreams": [upstream_to_public_dict(record) for record in repo.list_owned_by(user_id)],
    }


@router.post("/account/api/upstreams")
async def create_upstream(
    request: Request,
    payload: UpstreamCreateInput,
    discord_config: DiscordSelfServiceConfig = Depends(get_discord_self_service_config),
    app_config: AppConfig = Depends(get_config),
    db: Database = Depends(get_db),
):
    require_discord_enabled(discord_config)
    upstreams_config = _require_upstreams_enabled(app_config)
    user_id = _require_login(request, discord_config)
    ensure_self_service_account_active(db, user_id)
    repo = NovelAIUpstreamRepository(db)
    record = repo.create_owned(
        owner_user_id=user_id,
        label=payload.label,
        api_key=payload.api_key,
        enabled=payload.enabled,
        max_per_user=upstreams_config.max_per_user,
    )
    _reload_upstream(request, record.id)
    return {"upstream": upstream_to_public_dict(record)}


@router.patch("/account/api/upstreams/{upstream_id}")
async def update_upstream(
    request: Request,
    upstream_id: str,
    payload: UpstreamUpdateInput,
    discord_config: DiscordSelfServiceConfig = Depends(get_discord_self_service_config),
    app_config: AppConfig = Depends(get_config),
    db: Database = Depends(get_db),
):
    require_discord_enabled(discord_config)
    _require_upstreams_enabled(app_config)
    user_id = _require_login(request, discord_config)
    ensure_self_service_account_active(db, user_id)
    repo = NovelAIUpstreamRepository(db)
    owned = _require_owned(repo, upstream_id, user_id)
    record = repo.update(upstream_id, api_key=payload.api_key, enabled=payload.enabled)
    if payload.enabled is False and owned.enabled:
        _notify_if_referenced(request, db, upstream_id, action="disable")
    _reload_upstream(request, record.id)
    return {"upstream": upstream_to_public_dict(record)}


@router.delete("/account/api/upstreams/{upstream_id}")
async def delete_upstream(
    request: Request,
    upstream_id: str,
    discord_config: DiscordSelfServiceConfig = Depends(get_discord_self_service_config),
    app_config: AppConfig = Depends(get_config),
    db: Database = Depends(get_db),
):
    require_discord_enabled(discord_config)
    _require_upstreams_enabled(app_config)
    user_id = _require_login(request, discord_config)
    ensure_self_service_account_active(db, user_id)
    repo = NovelAIUpstreamRepository(db)
    _require_owned(repo, upstream_id, user_id)
    try:
        repo.delete(upstream_id)
    except UpstreamConflict as exc:
        # 剥掉 references 详情，避免把其他用户的数字 ID 泄露给普通用户。
        _notify_if_referenced(request, db, upstream_id, action="delete")
        raise UpstreamConflict(
            "该上游已被管理员指定给特定用户使用，暂时无法删除。你可以先停用它，或联系管理员。"
        ) from exc
    _reload_upstream(request, upstream_id)
    return {"ok": True}


def _require_upstreams_enabled(config: AppConfig) -> SelfServiceUpstreamsConfig:
    upstreams = config.self_service.upstreams
    if not upstreams.enabled:
        raise HTTPException(status_code=404, detail={"message": "自助上游上传未启用"})
    return upstreams


def _require_login(request: Request, config: DiscordSelfServiceConfig) -> int:
    user_id = current_self_service_user_id(request, config.session_secret)
    if user_id is None:
        raise HTTPException(status_code=401, detail={"message": "需要登录"})
    return user_id


def _require_owned(
    repo: NovelAIUpstreamRepository,
    upstream_id: str,
    user_id: int,
) -> NovelAIUpstreamRecord:
    """唯一的归属校验点：只认 owner_user_id 列，绝不解析 ID 字符串。"""
    record = repo.get(upstream_id)
    if record is None or record.owner_user_id != user_id:
        raise UpstreamNotFound()
    return record


def _notify_if_referenced(request: Request, db: Database, upstream_id: str, *, action: str) -> None:
    """停用/删除仍被白名单引用的自助 key 时通知管理员，避免受影响用户无感知。"""
    conflicts = NovelAIUpstreamRepository(db).find_allowed_upstream_references(upstream_id)
    if not conflicts:
        return
    notification_repo: AdminNotificationRepository = request.app.state.admin_notifications
    notification_repo.create(
        event_type="self_service_upstream_referenced",
        title="自助上游仍被用户引用",
        content=(
            f"用户对自助上游 {upstream_id} 执行了「{action}」，但该上游仍被用户或用户组白名单引用，"
            "可能影响被指定用户的请求，请检查白名单配置。"
        ),
        metadata={
            "upstream_id": upstream_id,
            "action": action,
            "references": conflicts,
        },
    )


def _reload_upstream(request: Request, upstream_id: str) -> None:
    request.app.state.upstream_runtime.reload_upstream(upstream_id)
