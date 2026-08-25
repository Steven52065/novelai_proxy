from __future__ import annotations

from fastapi import HTTPException, Request

from ..config import DiscordSelfServiceConfig
from ..database import Database
from ..signed_tokens import verify_payload

SESSION_COOKIE = "novelai_proxy_self_service_session"


def require_discord_enabled(config: DiscordSelfServiceConfig) -> DiscordSelfServiceConfig:
    if not config.enabled:
        raise HTTPException(status_code=404, detail={"message": "Discord 自助服务未启用"})
    return config


def current_self_service_user_id(request: Request, secret: str) -> int | None:
    payload = verify_payload(request.cookies.get(SESSION_COOKIE), secret)
    if payload is None:
        return None
    try:
        return int(payload["user_id"])
    except (KeyError, TypeError, ValueError):
        return None


def ensure_self_service_account_active(db: Database, user_id: int) -> None:
    user = db.query_one(
        "SELECT is_active, deleted_at FROM users WHERE id = ?",
        (user_id,),
    )
    if user is None or user["deleted_at"] is not None:
        raise HTTPException(status_code=403, detail={"message": "账号不可用"})
    if not int(user["is_active"]):
        raise HTTPException(status_code=403, detail={"message": "账号已被禁用"})
