from __future__ import annotations

import logging
import re
import secrets
import traceback
from typing import Any, NoReturn
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..allowlists import AllowedEndpoints, AllowedUpstreams
from ..api_key_flash import ApiKeyFlashStore
from ..config import DiscordSelfServiceConfig
from ..database import Database
from ..deps import (
    get_db,
    get_discord_oauth_client,
    get_discord_self_service_config,
    get_free_small_daily_limit_manager,
    get_quota_manager,
)
from ..free_small_daily_limit import FreeSmallDailyLimitManager
from ..image_format_policies import (
    IMAGE_FORMAT_POLICY_CHOICES,
    image_format_policy_label,
    normalize_image_format_policy,
)
from ..logging_utils import json_dumps, logger
from ..quota_manager import QuotaManager
from ..templating import templates
from ..users import reset_api_key
from .accounts import DiscordProfile, login_or_register_discord_user
from .discord import DiscordOAuthClient
from .session import expiring_payload, sign_payload, verify_payload


router = APIRouter()
OAUTH_STATE_COOKIE = "novelai_proxy_discord_oauth_state"
SESSION_COOKIE = "novelai_proxy_self_service_session"
API_KEY_FLASH_COOKIE = "novelai_proxy_self_service_key_flash"
OAUTH_STATE_TTL_SECONDS = 5 * 60
SESSION_TTL_SECONDS = 7 * 24 * 3600
SENSITIVE_OAUTH_FIELDS = {"access_token", "refresh_token", "client_secret", "token", "authorization"}
SENSITIVE_QUERY_FIELDS = {"code", "state", "access_token", "refresh_token", "client_secret", "token"}
REDACTED = "[redacted]"
_api_key_flash = ApiKeyFlashStore(cookie_name=API_KEY_FLASH_COOKIE, state_attr="self_service_api_key_flash_store")


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(
    request: Request,
    config: DiscordSelfServiceConfig = Depends(get_discord_self_service_config),
):
    _require_discord_enabled(config)
    return templates.TemplateResponse(request, "signup.html", {"active": "signup"})


@router.get("/auth/discord/start")
async def discord_start(
    request: Request,
    config: DiscordSelfServiceConfig = Depends(get_discord_self_service_config),
    oauth: DiscordOAuthClient = Depends(get_discord_oauth_client),
):
    _require_discord_enabled(config)
    state = secrets.token_urlsafe(24)
    response = RedirectResponse(
        oauth.authorization_url(redirect_uri=config.redirect_uri, state=state),
        status_code=303,
    )
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        sign_payload(expiring_payload(OAUTH_STATE_TTL_SECONDS, state=state), config.session_secret),
        max_age=OAUTH_STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/auth/discord/callback")
async def discord_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    config: DiscordSelfServiceConfig = Depends(get_discord_self_service_config),
    oauth: DiscordOAuthClient = Depends(get_discord_oauth_client),
    db: Database = Depends(get_db),
    quota_manager: QuotaManager = Depends(get_quota_manager),
):
    _require_discord_enabled(config)
    saved_state = verify_payload(request.cookies.get(OAUTH_STATE_COOKIE), config.session_secret)
    if not code or not state or saved_state is None or saved_state.get("state") != state:
        raise HTTPException(status_code=400, detail={"message": "Invalid Discord OAuth state"})

    try:
        token_payload = await oauth.exchange_code(code=code, redirect_uri=config.redirect_uri)
    except Exception as exc:
        _raise_discord_oauth_failure("exchange_code", exc)

    if not isinstance(token_payload, dict):
        _raise_discord_oauth_failure(
            "parse_token_response",
            TypeError("Discord token response is not a JSON object"),
            extra={"token_response_type": type(token_payload).__name__},
        )
    access_token = str(token_payload.get("access_token") or "")
    if not access_token:
        _raise_discord_oauth_failure(
            "parse_token_response",
            ValueError("missing access_token"),
            extra={"token_response_keys": sorted(str(key) for key in token_payload.keys())},
        )

    try:
        user_payload = await oauth.fetch_user(access_token=access_token)
    except Exception as exc:
        _raise_discord_oauth_failure("fetch_user", exc)

    try:
        guilds_payload = await oauth.fetch_guilds(access_token=access_token)
    except Exception as exc:
        _raise_discord_oauth_failure("fetch_guilds", exc)

    if str(config.required_guild_id) not in {str(guild.get("id")) for guild in guilds_payload}:
        raise HTTPException(status_code=403, detail={"message": "Discord user is not in the required guild"})

    profile = _profile_from_payload(user_payload)
    login = login_or_register_discord_user(
        db,
        quota_manager,
        default_group_id=int(config.default_group_id),
        profile=profile,
    )
    response = RedirectResponse("/account", status_code=303)
    _set_session_cookie(response, config.session_secret, login.user_id)
    response.delete_cookie(OAUTH_STATE_COOKIE)
    if login.api_key is not None:
        _api_key_flash.set_flash(response, request, login.api_key, owner_id=login.user_id)
    return response


@router.get("/account", response_class=HTMLResponse)
async def account_page(
    request: Request,
    config: DiscordSelfServiceConfig = Depends(get_discord_self_service_config),
    db: Database = Depends(get_db),
    quota_manager: QuotaManager = Depends(get_quota_manager),
    free_small_daily_limit_manager: FreeSmallDailyLimitManager = Depends(get_free_small_daily_limit_manager),
):
    _require_discord_enabled(config)
    user_id = _current_self_service_user_id(request, config.session_secret)
    if user_id is None:
        return RedirectResponse("/signup", status_code=303)
    user = db.query_one(
        """
        SELECT u.id, u.name, u.group_id, u.tier, u.is_active, u.free_small_only,
               u.allowed_endpoints, u.allowed_upstreams, u.image_format_policy, u.deleted_at,
               g.name AS group_name, g.is_active AS group_is_active,
               l.discord_username, l.discord_global_name, l.discord_avatar,
               COALESCE(q.reset_period, 'month') AS quota_reset_period,
               COALESCE(q.reset_day, 1) AS quota_reset_day
        FROM users u
        LEFT JOIN user_groups g ON g.id = u.group_id
        LEFT JOIN discord_user_links l ON l.user_id = u.id
        LEFT JOIN user_anlas_quota q ON q.user_id = u.id
        WHERE u.id = ?
        """,
        (user_id,),
    )
    if user is None or user["deleted_at"] is not None:
        raise HTTPException(status_code=403, detail={"message": "Account is unavailable"})
    if not int(user["is_active"]):
        raise HTTPException(status_code=403, detail={"message": "Account is disabled"})
    quota = quota_manager.get_snapshot(user_id)
    free_small_daily = free_small_daily_limit_manager.get_snapshot(user_id)
    has_api_key_flash = API_KEY_FLASH_COOKIE in request.cookies
    new_api_key = _api_key_flash.pop_flash(request, owner_id=user_id)
    response = templates.TemplateResponse(
        request,
        "account.html",
        {
            "user": _account_user_to_dict(user),
            "quota": quota,
            "quota_reset": {
                "period": user["quota_reset_period"],
                "day": user["quota_reset_day"],
            },
            "free_small_daily": free_small_daily,
            "image_format_policy_choices": IMAGE_FORMAT_POLICY_CHOICES,
            "new_api_key": new_api_key,
        },
    )
    if has_api_key_flash:
        response.delete_cookie(API_KEY_FLASH_COOKIE)
    return response


@router.post("/account/image-format-policy")
async def account_update_image_format_policy(
    request: Request,
    image_format_policy: str = Form(...),
    config: DiscordSelfServiceConfig = Depends(get_discord_self_service_config),
    db: Database = Depends(get_db),
):
    _require_discord_enabled(config)
    user_id = _current_self_service_user_id(request, config.session_secret)
    if user_id is None:
        return RedirectResponse("/signup", status_code=303)
    _ensure_self_service_account_active(db, user_id)
    try:
        normalized = normalize_image_format_policy(image_format_policy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc
    db.execute(
        "UPDATE users SET image_format_policy = ? WHERE id = ? AND deleted_at IS NULL",
        (normalized, user_id),
    )
    return RedirectResponse("/account", status_code=303)


@router.post("/account/reset-key")
async def account_reset_key(
    request: Request,
    config: DiscordSelfServiceConfig = Depends(get_discord_self_service_config),
    db: Database = Depends(get_db),
):
    _require_discord_enabled(config)
    user_id = _current_self_service_user_id(request, config.session_secret)
    if user_id is None:
        return RedirectResponse("/signup", status_code=303)
    _ensure_self_service_account_active(db, user_id)
    api_key = reset_api_key(db, user_id)
    response = RedirectResponse("/account", status_code=303)
    _api_key_flash.set_flash(response, request, api_key, owner_id=user_id)
    return response


@router.post("/account/logout")
async def account_logout(
    config: DiscordSelfServiceConfig = Depends(get_discord_self_service_config),
):
    _require_discord_enabled(config)
    response = RedirectResponse("/signup", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(API_KEY_FLASH_COOKIE)
    return response


def _profile_from_payload(payload: dict) -> DiscordProfile:
    user_id = str(payload.get("id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=502, detail={"message": "Discord user payload missing id"})
    username = _optional_str(payload.get("username"))
    global_name = _optional_str(payload.get("global_name"))
    avatar = _optional_str(payload.get("avatar"))
    return DiscordProfile(user_id=user_id, username=username, global_name=global_name, avatar=avatar)


def _account_user_to_dict(row) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "group_id": row["group_id"],
        "group_name": row["group_name"],
        "group_is_active": row["group_is_active"],
        "tier": row["tier"],
        "is_active": bool(row["is_active"]),
        "free_small_only": bool(row["free_small_only"]),
        "allowed_endpoints_list": AllowedEndpoints.parse(row["allowed_endpoints"]).as_list(),
        "allowed_upstreams_list": AllowedUpstreams.parse(row["allowed_upstreams"]).as_list(),
        "image_format_policy": normalize_image_format_policy(row["image_format_policy"]),
        "image_format_policy_label": image_format_policy_label(row["image_format_policy"]),
        "discord_username": row["discord_username"],
        "discord_global_name": row["discord_global_name"],
        "discord_avatar": row["discord_avatar"],
    }


def _require_discord_enabled(config: DiscordSelfServiceConfig) -> DiscordSelfServiceConfig:
    if not config.enabled:
        raise HTTPException(status_code=404, detail={"message": "Discord self-service is disabled"})
    return config


def _raise_discord_oauth_failure(phase: str, exc: Exception, *, extra: dict[str, Any] | None = None) -> NoReturn:
    _log_discord_oauth_failure(phase, exc, extra=extra)
    raise HTTPException(status_code=502, detail={"message": "Discord OAuth request failed"}) from exc


def _log_discord_oauth_failure(phase: str, exc: Exception, *, extra: dict[str, Any] | None = None) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return

    context: dict[str, Any] = {
        "phase": phase,
        "error_type": type(exc).__name__,
        "error": _redact_sensitive_text(str(exc)),
    }
    if extra:
        context.update(_redact_oauth_payload(extra))

    request = getattr(exc, "request", None)
    response = getattr(exc, "response", None)
    if isinstance(exc, httpx.RequestError):
        request = exc.request
    if isinstance(exc, httpx.HTTPStatusError):
        request = exc.request
        response = exc.response

    if request is not None:
        context["request_method"] = getattr(request, "method", None)
        context["request_url"] = _safe_oauth_url(str(getattr(request, "url", "")))
    if response is not None:
        context["status_code"] = getattr(response, "status_code", None)
        context["reason_phrase"] = getattr(response, "reason_phrase", None)
        context["response_body"] = _safe_response_body(response)

    context["traceback"] = _redact_sensitive_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    logger.debug("discord oauth failure details=%s", json_dumps(context))


def _safe_response_body(response) -> Any:
    try:
        payload = response.json()
    except ValueError:
        text = getattr(response, "text", "")
        return _redact_sensitive_text(str(text)[:2000])
    return _redact_oauth_payload(payload)


def _redact_oauth_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if str(key).lower() in SENSITIVE_OAUTH_FIELDS else _redact_oauth_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_oauth_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    return value


def _safe_oauth_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _redact_sensitive_text(value: str) -> str:
    if not value:
        return value
    query_names = "|".join(re.escape(name) for name in sorted(SENSITIVE_QUERY_FIELDS))
    field_names = "|".join(re.escape(name) for name in sorted(SENSITIVE_OAUTH_FIELDS))
    redacted = re.sub(rf"(?i)([?&](?:{query_names})=)[^&\s'\"<>]+", rf"\1{REDACTED}", value)
    redacted = re.sub(rf"(?i)\b((?:{query_names})=)[^&\s'\"<>]+", rf"\1{REDACTED}", redacted)
    redacted = re.sub(rf"(?i)([\"'](?:{field_names})[\"']\s*:\s*[\"'])(.*?)([\"'])", rf"\1{REDACTED}\3", redacted)
    redacted = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", rf"\1{REDACTED}", redacted)
    return redacted


def _set_session_cookie(response, secret: str, user_id: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        sign_payload(expiring_payload(SESSION_TTL_SECONDS, user_id=user_id), secret),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )


def _current_self_service_user_id(request: Request, secret: str) -> int | None:
    payload = verify_payload(request.cookies.get(SESSION_COOKIE), secret)
    if payload is None:
        return None
    try:
        return int(payload["user_id"])
    except (KeyError, TypeError, ValueError):
        return None


def _ensure_self_service_account_active(db: Database, user_id: int) -> None:
    user = db.query_one(
        "SELECT is_active, deleted_at FROM users WHERE id = ?",
        (user_id,),
    )
    if user is None or user["deleted_at"] is not None:
        raise HTTPException(status_code=403, detail={"message": "Account is unavailable"})
    if not int(user["is_active"]):
        raise HTTPException(status_code=403, detail={"message": "Account is disabled"})


def _optional_str(value) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None
