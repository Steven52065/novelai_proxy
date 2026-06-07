from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..database import Database
from ..users import (
    UserGroupInput,
    UserGroupUpdateInput,
    create_group,
    delete_or_disable_group,
    get_group,
    list_groups,
    sync_group_members,
    update_group,
)
from ..users.service import parse_allowed_endpoints, parse_allowed_upstreams
from .auth import require_admin
from .common import ALLOWED_ENDPOINT_CHOICES, DEFAULT_ALLOWED_ENDPOINTS, row_to_dict


api_router = APIRouter(prefix="/admin/api")


class CreateUserGroupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    is_active: bool = True
    default_tier: str = Field(default="normal", pattern="^(normal|vip)$")
    default_free_small_only: bool = True
    default_allowed_endpoints: list[str] = Field(default_factory=lambda: [DEFAULT_ALLOWED_ENDPOINTS])
    default_allowed_upstreams: list[str] = Field(default_factory=list)
    default_anlas_total: int = Field(default=0, ge=0)
    default_reset_period: str = Field(default="month", pattern="^(month|week|day|never)$")
    default_reset_day: int = Field(default=1, ge=0, le=28)


class UpdateUserGroupRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None
    default_tier: str | None = Field(default=None, pattern="^(normal|vip)$")
    default_free_small_only: bool | None = None
    default_allowed_endpoints: list[str] | None = None
    default_allowed_upstreams: list[str] | None = None
    default_anlas_total: int | None = Field(default=None, ge=0)
    default_reset_period: str | None = Field(default=None, pattern="^(month|week|day|never)$")
    default_reset_day: int | None = Field(default=None, ge=0, le=28)


class SyncGroupMembersRequest(BaseModel):
    fields: list[Literal["tier", "free_small_only", "allowed_endpoints", "allowed_upstreams", "anlas_quota"]] = Field(
        default_factory=list
    )


@api_router.get("/user-groups", dependencies=[Depends(require_admin)])
async def list_user_groups(request: Request):
    db: Database = request.app.state.db
    return {"groups": [_group_row_to_dict(row) for row in list_groups(db)]}


@api_router.post("/user-groups", dependencies=[Depends(require_admin)])
async def create_user_group(payload: CreateUserGroupRequest, request: Request):
    db: Database = request.app.state.db
    _validate_allowed_endpoints(payload.default_allowed_endpoints)
    _validate_allowed_upstreams(payload.default_allowed_upstreams, request)
    group_id = create_group(
        db,
        UserGroupInput(
            name=payload.name,
            is_active=payload.is_active,
            default_tier=payload.default_tier,
            default_free_small_only=payload.default_free_small_only,
            default_allowed_endpoints=payload.default_allowed_endpoints,
            default_allowed_upstreams=payload.default_allowed_upstreams,
            default_anlas_total=payload.default_anlas_total,
            default_reset_period=payload.default_reset_period,
            default_reset_day=payload.default_reset_day,
        ),
    )
    _notify_dashboard_change(request)
    return {"group_id": group_id, "group": _group_row_to_dict(get_group(db, group_id))}


@api_router.get("/user-groups/{group_id}", dependencies=[Depends(require_admin)])
async def get_user_group(group_id: int, request: Request):
    db: Database = request.app.state.db
    return {"group": _group_row_to_dict(get_group(db, group_id))}


@api_router.patch("/user-groups/{group_id}", dependencies=[Depends(require_admin)])
async def patch_user_group(group_id: int, payload: UpdateUserGroupRequest, request: Request):
    db: Database = request.app.state.db
    if payload.default_allowed_endpoints is not None:
        _validate_allowed_endpoints(payload.default_allowed_endpoints)
    if payload.default_allowed_upstreams is not None:
        _validate_allowed_upstreams(payload.default_allowed_upstreams, request)
    changed = update_group(
        db,
        group_id,
        UserGroupUpdateInput(
            name=payload.name,
            is_active=payload.is_active,
            default_tier=payload.default_tier,
            default_free_small_only=payload.default_free_small_only,
            default_allowed_endpoints=payload.default_allowed_endpoints,
            default_allowed_upstreams=payload.default_allowed_upstreams,
            default_anlas_total=payload.default_anlas_total,
            default_reset_period=payload.default_reset_period,
            default_reset_day=payload.default_reset_day,
        ),
    )
    if changed:
        _notify_dashboard_change(request)
    return {"ok": True}


@api_router.delete("/user-groups/{group_id}", dependencies=[Depends(require_admin)])
async def delete_user_group(group_id: int, request: Request):
    db: Database = request.app.state.db
    delete_or_disable_group(db, group_id)
    _notify_dashboard_change(request)
    return {"ok": True}


@api_router.post("/user-groups/{group_id}/sync-members", dependencies=[Depends(require_admin)])
async def sync_user_group_members(group_id: int, payload: SyncGroupMembersRequest, request: Request):
    updated_users = sync_group_members(
        request.app.state.db,
        request.app.state.quota_manager,
        group_id,
        payload.fields,
    )
    if updated_users:
        _notify_dashboard_change(request)
    return {"ok": True, "updated_users": updated_users}


def _group_row_to_dict(row):
    data = row_to_dict(row)
    data["default_allowed_endpoints_list"] = parse_allowed_endpoints(data.get("default_allowed_endpoints"))
    data["default_allowed_upstreams_list"] = parse_allowed_upstreams(data.get("default_allowed_upstreams"))
    return data


def _validate_allowed_endpoints(allowed_endpoints: list[str] | None) -> None:
    if allowed_endpoints is None:
        return
    normalized = [item.strip() for item in allowed_endpoints if item.strip()]
    if not normalized:
        raise HTTPException(status_code=400, detail={"message": "At least one endpoint must be allowed"})
    unknown = sorted({item for item in normalized if item not in ALLOWED_ENDPOINT_CHOICES})
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"message": f"Unknown endpoint: {', '.join(unknown)}"},
        )


def _upstream_choices(request: Request) -> list[str]:
    clients = getattr(request.app.state, "upstream_clients", None)
    if isinstance(clients, dict) and clients:
        return list(clients.keys())
    return ["default"]


def _validate_allowed_upstreams(allowed_upstreams: list[str] | None, request: Request) -> None:
    if not allowed_upstreams:
        return
    valid_upstreams = set(_upstream_choices(request))
    unknown = sorted({item.strip() for item in allowed_upstreams if item.strip()} - valid_upstreams)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"message": f"Unknown upstream id: {', '.join(unknown)}"},
        )


def _notify_dashboard_change(request: Request) -> None:
    event_bus = getattr(request.app.state, "dashboard_events", None)
    if event_bus is not None:
        event_bus.notify_nowait()
