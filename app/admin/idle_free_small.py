from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .auth import require_admin_or_session


api_router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin_or_session)])


@api_router.get("/idle-free-small-settings")
async def idle_free_small_settings(request: Request):
    config = request.app.state.config.idle_free_small
    return {
        "occupancy_threshold_percent": config.occupancy_threshold_percent,
        "min_idle_seconds": config.min_idle_seconds,
    }
