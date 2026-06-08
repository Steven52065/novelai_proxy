from __future__ import annotations

import logging
import re
import secrets
import time
import traceback
from typing import Any, NoReturn
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..admin.common import templates
from ..database import Database
from ..logging_utils import json_dumps, logger
from ..users import reset_api_key
from ..users.service import parse_allowed_endpoints, parse_allowed_upstreams
from .accounts import DiscordProfile, login_or_register_discord_user
from .discord import DiscordOAuthClient
from .session import expiring_payload, sign_payload, verify_payload


router = APIRouter()
OAUTH_STATE_COOKIE = "novelai_proxy_discord_oauth_state"
SESSION_COOKIE = "novelai_proxy_self_service_session"
API_KEY_FLASH_COOKIE = "novelai_proxy_self_service_key_flash"
OAUTH_STATE_TTL_SECONDS = 5 * 60
SESSION_TTL_SECONDS = 7 * 24 * 3600
API_KEY_FLASH_TTL_SECONDS = 5 * 60
SENSITIVE_OAUTH_FIELDS = {"access_token", "refresh_token", "client_secret", "token", "authorization"}
SENSITIVE_QUERY_FIELDS = {"code", "state", "access_token", "refresh_token", "client_secret", "token"}
REDACTED = "[redacted]"


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    _require_discord_enabled(request)
    return templates.TemplateResponse(request, "signup.html", {"active": "signup"})


@router.get("/auth/discord/start")
async def discord_start(request: Request):
    config = _require_discord_enabled(request)
    state = secrets.token_urlsafe(24)
    response = RedirectResponse(
        _discord_client(request).authorization_url(redirect_uri=config.redirect_uri, state=state),
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
async def discord_callback(request: Request, code: str | None = None, state: str | None = None):
    config = _require_discord_enabled(request)
    saved_state = verify_payload(request.cookies.get(OAUTH_STATE_COOKIE), config.session_secret)
    if not code or not state or saved_state is None or saved_state.get("state") != state:
        raise HTTPException(status_code=400, detail={"message": "Invalid Discord OAuth state"})

    oauth = _discord_client(request)
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
        request.app.state.db,
        request.app.state.quota_manager,
        default_group_id=int(config.default_group_id),
        profile=profile,
    )
    response = RedirectResponse("/account", status_code=303)
    _set_session_cookie(response, config.session_secret, login.user_id)
    response.delete_cookie(OAUTH_STATE_COOKIE)
    if login.api_key is not None:
        _set_api_key_flash(response, request, login.user_id, login.api_key)
    return response


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    config = _require_discord_enabled(request)
    user_id = _current_self_service_user_id(request, config.session_secret)
    if user_id is None:
        return RedirectResponse("/signup", status_code=303)
    db: Database = request.app.state.db
    user = db.query_one(
        """
        SELECT u.id, u.name, u.group_id, u.tier, u.is_active, u.free_small_only,
               u.allowed_endpoints, u.allowed_upstreams, u.deleted_at,
               g.name AS group_name, g.is_active AS group_is_active,
               l.discord_username, l.discord_global_name, l.discord_avatar
        FROM users u
        LEFT JOIN user_groups g ON g.id = u.group_id
        LEFT JOIN discord_user_links l ON l.user_id = u.id
        WHERE u.id = ?
        """,
        (user_id,),
    )
    if user is None or user["deleted_at"] is not None:
        raise HTTPException(status_code=403, detail={"message": "Account is unavailable"})
    if not int(user["is_active"]):
        raise HTTPException(status_code=403, detail={"message": "Account is disabled"})
    quota = request.app.state.quota_manager.get_snapshot(user_id)
    has_api_key_flash = API_KEY_FLASH_COOKIE in request.cookies
    new_api_key = _pop_api_key_flash(request, user_id)
    response = templates.TemplateResponse(
        request,
        "account.html",
        {
            "user": _account_user_to_dict(user),
            "quota": quota,
            "new_api_key": new_api_key,
        },
    )
    if has_api_key_flash:
        response.delete_cookie(API_KEY_FLASH_COOKIE)
    return response


@router.post("/account/reset-key")
async def account_reset_key(request: Request):
    config = _require_discord_enabled(request)
    user_id = _current_self_service_user_id(request, config.session_secret)
    if user_id is None:
        return RedirectResponse("/signup", status_code=303)
    _ensure_self_service_account_active(request.app.state.db, user_id)
    api_key = reset_api_key(request.app.state.db, user_id)
    response = RedirectResponse("/account", status_code=303)
    _set_api_key_flash(response, request, user_id, api_key)
    return response


@router.post("/account/logout")
async def account_logout(request: Request):
    _require_discord_enabled(request)
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
        "allowed_endpoints_list": parse_allowed_endpoints(row["allowed_endpoints"]),
        "allowed_upstreams_list": parse_allowed_upstreams(row["allowed_upstreams"]),
        "discord_username": row["discord_username"],
        "discord_global_name": row["discord_global_name"],
        "discord_avatar": row["discord_avatar"],
    }


def _require_discord_enabled(request: Request):
    config = request.app.state.config.self_service.discord
    if not config.enabled:
        raise HTTPException(status_code=404, detail={"message": "Discord self-service is disabled"})
    return config


def _discord_client(request: Request) -> DiscordOAuthClient:
    return request.app.state.discord_oauth_client


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


def _set_api_key_flash(response, request: Request, user_id: int, api_key: str) -> None:
    token = secrets.token_urlsafe(24)
    _cleanup_api_key_flash_store(request)
    _api_key_flash_store(request)[token] = (user_id, api_key, time.monotonic() + API_KEY_FLASH_TTL_SECONDS)
    response.set_cookie(
        API_KEY_FLASH_COOKIE,
        token,
        max_age=API_KEY_FLASH_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )


def _pop_api_key_flash(request: Request, user_id: int) -> str | None:
    token = request.cookies.get(API_KEY_FLASH_COOKIE)
    if not token:
        return None
    _cleanup_api_key_flash_store(request)
    value = _api_key_flash_store(request).pop(token, None)
    if value is None:
        return None
    stored_user_id, api_key, expires_at = value
    if time.monotonic() > expires_at:
        return None
    if int(stored_user_id) != user_id:
        return None
    return api_key


def _api_key_flash_store(request: Request) -> dict[str, tuple[int, str, float]]:
    store = getattr(request.app.state, "self_service_api_key_flash_store", None)
    if store is None:
        store = {}
        request.app.state.self_service_api_key_flash_store = store
    return store


def _cleanup_api_key_flash_store(request: Request) -> None:
    now = time.monotonic()
    store = _api_key_flash_store(request)
    for token, (_, _, expires_at) in list(store.items()):
        if expires_at <= now:
            store.pop(token, None)


def _optional_str(value) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None
