from __future__ import annotations

from fastapi import APIRouter

from .auth import web_router as auth_web_router
from .dashboard import api_router as dashboard_api_router
from .dashboard import web_router as dashboard_web_router
from .database import api_router as database_api_router
from .database import web_router as database_web_router
from .groups import api_router as groups_api_router
from .logs import api_router as logs_api_router
from .logs import web_router as logs_web_router
from .users import api_router as users_api_router
from .users import web_router as users_web_router


router = APIRouter(tags=["admin"])

# 路由聚合层只负责挂载子模块，避免单个 routes 文件继续膨胀。
for child_router in (
    users_api_router,
    groups_api_router,
    logs_api_router,
    dashboard_api_router,
    database_api_router,
    dashboard_web_router,
    auth_web_router,
    users_web_router,
    logs_web_router,
    database_web_router,
):
    router.include_router(child_router)
