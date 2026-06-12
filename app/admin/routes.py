from __future__ import annotations

# 兼容既有导入路径；实际路由已按功能拆分到 admin 子模块。
from .auth import (
    AdminLoginRequired,
    admin_login_required_handler,
    set_admin_session_cookie,
    valid_admin_session,
)
from .router import router


__all__ = [
    "router",
    "AdminLoginRequired",
    "admin_login_required_handler",
    "set_admin_session_cookie",
    "valid_admin_session",
]
