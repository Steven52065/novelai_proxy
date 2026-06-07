from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..admin.common import templates
from ..database import Database, utc_now_iso
from ..users import CreateUserInput, create_user, get_enabled_group, group_defaults, reset_api_key
from ..users.service import parse_allowed_endpoints, parse_allowed_upstreams
from .discord import DiscordOAuthClient
from .session import expiring_payload, sign_payload, verify_payload


router = APIRouter()
OAUTH_STATE_COOKIE = "novelai_proxy_discord_oauth_state"
SESSION_COOKIE = "novelai_proxy_self_service_session"
API_KEY_FLASH_COOKIE = "novelai_proxy_self_service_key_flash"
OAUTH_STATE_TTL_SECONDS = 5 * 60
SESSION_TTL_SECONDS = 7 * 24 * 3600
API_KEY_FLASH_TTL_SECONDS = 5 * 60


@dataclass(frozen=True)
class DiscordProfile:
    user_id: str
    username: str | None
    global_name: str | None
    avatar: str | None


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
        access_token = str(token_payload.get("access_token") or "")
        if not access_token:
            raise ValueError("missing access_token")
        user_payload = await oauth.fetch_user(access_token=access_token)
        guilds_payload = await oauth.fetch_guilds(access_token=access_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"message": "Discord OAuth request failed"}) from exc

    if str(config.required_guild_id) not in {str(guild.get("id")) for guild in guilds_payload}:
        raise HTTPException(status_code=403, detail={"message": "Discord user is not in the required guild"})

    profile = _profile_from_payload(user_payload)
    login = _login_or_register_discord_user(request, profile)
    response = RedirectResponse("/account", status_code=303)
    _set_session_cookie(response, config.session_secret, login.user_id)
    response.delete_cookie(OAUTH_STATE_COOKIE)
    if login.api_key is not None:
        _set_api_key_flash(response, request, login.api_key)
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
    quota = request.app.state.quota_manager.get_snapshot(user_id)
    new_api_key = _pop_api_key_flash(request)
    response = templates.TemplateResponse(
        request,
        "account.html",
        {
            "user": _account_user_to_dict(user),
            "quota": quota,
            "new_api_key": new_api_key,
        },
    )
    if new_api_key is not None:
        response.delete_cookie(API_KEY_FLASH_COOKIE)
    return response


@router.post("/account/reset-key")
async def account_reset_key(request: Request):
    config = _require_discord_enabled(request)
    user_id = _current_self_service_user_id(request, config.session_secret)
    if user_id is None:
        return RedirectResponse("/signup", status_code=303)
    api_key = reset_api_key(request.app.state.db, user_id)
    response = RedirectResponse("/account", status_code=303)
    _set_api_key_flash(response, request, api_key)
    return response


@router.post("/account/logout")
async def account_logout(request: Request):
    _require_discord_enabled(request)
    response = RedirectResponse("/signup", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@dataclass(frozen=True)
class DiscordLoginResult:
    user_id: int
    api_key: str | None


def _login_or_register_discord_user(request: Request, profile: DiscordProfile) -> DiscordLoginResult:
    db: Database = request.app.state.db
    row = db.query_one(
        """
        SELECT l.id AS link_id, l.user_id, l.discord_username, l.discord_global_name, l.discord_avatar,
               u.name, u.deleted_at
        FROM discord_user_links l
        JOIN users u ON u.id = l.user_id
        WHERE l.discord_user_id = ?
        """,
        (profile.user_id,),
    )
    if row is not None:
        if row["deleted_at"] is not None:
            raise HTTPException(status_code=403, detail={"message": "Account was deleted; contact administrator"})
        _sync_existing_discord_link(db, row, profile)
        return DiscordLoginResult(user_id=int(row["user_id"]), api_key=None)

    config = request.app.state.config.self_service.discord
    group = get_enabled_group(db, int(config.default_group_id))
    defaults = group_defaults(group)
    created = create_user(
        db,
        request.app.state.quota_manager,
        CreateUserInput(
            name=discord_display_name(profile),
            group_id=int(config.default_group_id),
            tier=str(defaults["tier"]),
            free_small_only=bool(defaults["free_small_only"]),
            allowed_endpoints=list(defaults["allowed_endpoints"]),
            allowed_upstreams=list(defaults["allowed_upstreams"]),
            anlas_total=int(defaults["anlas_total"]),
            reset_period=str(defaults["reset_period"]),
            reset_day=int(defaults["reset_day"]),
        ),
    )
    now = utc_now_iso()
    db.execute(
        """
        INSERT INTO discord_user_links (
            user_id, discord_user_id, discord_username, discord_global_name,
            discord_avatar, created_at, last_login_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (created.user_id, profile.user_id, profile.username, profile.global_name, profile.avatar, now, now),
    )
    return DiscordLoginResult(user_id=created.user_id, api_key=created.api_key)


def _sync_existing_discord_link(db: Database, row, profile: DiscordProfile) -> None:
    old_names = discord_auto_names(
        user_id=profile.user_id,
        username=row["discord_username"],
        global_name=row["discord_global_name"],
    )
    new_name = discord_display_name(profile)
    if row["name"] in old_names and row["name"] != new_name:
        db.execute("UPDATE users SET name = ? WHERE id = ?", (new_name, int(row["user_id"])))
    db.execute(
        """
        UPDATE discord_user_links
        SET discord_username = ?,
            discord_global_name = ?,
            discord_avatar = ?,
            last_login_at = ?
        WHERE id = ?
        """,
        (profile.username, profile.global_name, profile.avatar, utc_now_iso(), int(row["link_id"])),
    )


def discord_display_name(profile: DiscordProfile) -> str:
    if profile.global_name:
        return f"Discord: {profile.global_name}"
    if profile.username:
        return f"Discord: @{profile.username}"
    return f"Discord 用户 {profile.user_id}"


def discord_auto_names(*, user_id: str, username: str | None, global_name: str | None) -> set[str]:
    names = {f"Discord 用户 {user_id}"}
    if username:
        names.add(f"Discord: @{username}")
    if global_name:
        names.add(f"Discord: {global_name}")
    return names


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


def _set_api_key_flash(response, request: Request, api_key: str) -> None:
    token = secrets.token_urlsafe(24)
    _cleanup_api_key_flash_store(request)
    _api_key_flash_store(request)[token] = (api_key, time.monotonic() + API_KEY_FLASH_TTL_SECONDS)
    response.set_cookie(
        API_KEY_FLASH_COOKIE,
        token,
        max_age=API_KEY_FLASH_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )


def _pop_api_key_flash(request: Request) -> str | None:
    token = request.cookies.get(API_KEY_FLASH_COOKIE)
    if not token:
        return None
    _cleanup_api_key_flash_store(request)
    value = _api_key_flash_store(request).pop(token, None)
    if value is None:
        return None
    api_key, expires_at = value
    if time.monotonic() > expires_at:
        return None
    return api_key


def _api_key_flash_store(request: Request) -> dict[str, tuple[str, float]]:
    store = getattr(request.app.state, "self_service_api_key_flash_store", None)
    if store is None:
        store = {}
        request.app.state.self_service_api_key_flash_store = store
    return store


def _cleanup_api_key_flash_store(request: Request) -> None:
    now = time.monotonic()
    store = _api_key_flash_store(request)
    for token, (_, expires_at) in list(store.items()):
        if expires_at <= now:
            store.pop(token, None)


def _optional_str(value) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None
