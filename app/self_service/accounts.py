from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection

from ..database import Database, utc_now_iso
from ..domain_errors import (
    SelfServiceAccountDeleted,
    SelfServiceAccountDisabled,
    SelfServiceRegistrationClosed,
    UserGroupDisabled,
    UserGroupNotFound,
)
from ..quota_manager import QuotaManager
from ..users import CreateUserInput, group_defaults, load_group_member_rate_limit_rules_with_connection
from ..users.service import insert_user_record


@dataclass(frozen=True)
class DiscordProfile:
    user_id: str
    username: str | None
    global_name: str | None
    avatar: str | None


@dataclass(frozen=True)
class DiscordLoginResult:
    user_id: int
    api_key: str | None


def disable_linked_discord_user(db: Database, *, discord_user_id: str) -> int | None:
    with db.transaction() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.is_active, u.deleted_at
            FROM discord_user_links l
            JOIN users u ON u.id = l.user_id
            WHERE l.discord_user_id = ?
            """,
            (discord_user_id,),
        ).fetchone()
        if row is None or row["deleted_at"] is not None or not int(row["is_active"]):
            return None
        user_id = int(row["id"])
        conn.execute(
            "UPDATE users SET is_active = 0, disabled_by_discord_verification = 1 WHERE id = ?",
            (user_id,),
        )
        return user_id


def login_or_register_discord_user(
    db: Database,
    quota_manager: QuotaManager,
    *,
    default_group_id: int,
    profile: DiscordProfile,
    allow_new_registration: bool,
) -> DiscordLoginResult:
    """登录或注册 Discord 自助账号。

    调用方必须已经完成配置要求的服务器/身份组验证：本函数把“能走到这里”视为验证通过，
    并据此恢复此前因验证失败而被停用的账号。

    allow_new_registration 为 False 时，仅允许已建立 discord_user_links 的老用户继续登录，
    新用户会直接抛出 SelfServiceRegistrationClosed，不会创建任何账号。
    """
    with db.transaction() as conn:
        row = _get_discord_link(conn, profile.user_id)
        if row is not None:
            if row["deleted_at"] is not None:
                raise SelfServiceAccountDeleted()
            if not int(row["is_active"]):
                # 只恢复由验证失败停用的账号。管理员手工停用的必须保持停用，
                # 否则用户重新走一次 Discord 登录就能绕过封禁。
                if not int(row["disabled_by_discord_verification"]):
                    raise SelfServiceAccountDisabled()
                conn.execute(
                    "UPDATE users SET is_active = 1, disabled_by_discord_verification = 0 WHERE id = ?",
                    (int(row["user_id"]),),
                )
            _sync_existing_discord_link(conn, row, profile)
            return DiscordLoginResult(user_id=int(row["user_id"]), api_key=None)

        if not allow_new_registration:
            raise SelfServiceRegistrationClosed()

        group = _get_enabled_group(conn, default_group_id)
        defaults = group_defaults(group)
        rate_limit_rules = load_group_member_rate_limit_rules_with_connection(conn, default_group_id)
        now = utc_now_iso()
        created = insert_user_record(
            conn,
            CreateUserInput(
                name=discord_display_name(profile),
                group_id=default_group_id,
                tier=str(defaults["tier"]),
                free_small_only=bool(defaults["free_small_only"]),
                free_small_daily_limit_enabled=bool(defaults["free_small_daily_limit_enabled"]),
                free_small_daily_limit=int(defaults["free_small_daily_limit"]),
                allowed_endpoints=list(defaults["allowed_endpoints"]),
                allowed_upstreams=list(defaults["allowed_upstreams"]),
                image_format_policy=str(defaults["image_format_policy"]),
                anlas_total=int(defaults["anlas_total"]),
                reset_period=str(defaults["reset_period"]),
                reset_day=int(defaults["reset_day"]),
                rate_limit_rules=rate_limit_rules,
            ),
            now=now,
        )
        quota_manager.create_or_update_with_connection(
            conn,
            created.user_id,
            int(defaults["anlas_total"]),
            str(defaults["reset_period"]),
            int(defaults["reset_day"]),
            now=now,
        )
        conn.execute(
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


def discord_display_name(profile: DiscordProfile) -> str:
    if profile.global_name:
        return f"Dc: {profile.global_name}"
    if profile.username:
        return f"Dc: @{profile.username}"
    return f"Discord 用户 {profile.user_id}"


def discord_auto_names(*, user_id: str, username: str | None, global_name: str | None) -> set[str]:
    names = {f"Discord 用户 {user_id}"}
    if username:
        names.add(f"Dc: @{username}")
        names.add(f"Discord: @{username}")
    if global_name:
        names.add(f"Dc: {global_name}")
        names.add(f"Discord: {global_name}")
    return names


def _get_discord_link(conn: Connection, discord_user_id: str):
    return conn.execute(
        """
        SELECT l.id AS link_id, l.user_id, l.discord_username, l.discord_global_name, l.discord_avatar,
               u.name, u.is_active, u.disabled_by_discord_verification, u.deleted_at
        FROM discord_user_links l
        JOIN users u ON u.id = l.user_id
        WHERE l.discord_user_id = ?
        """,
        (discord_user_id,),
    ).fetchone()


def _get_enabled_group(conn: Connection, group_id: int):
    row = conn.execute(
        """
        SELECT g.*,
               COUNT(u.id) AS member_count
        FROM user_groups g
        LEFT JOIN users u ON u.group_id = g.id AND u.deleted_at IS NULL
        WHERE g.id = ?
        GROUP BY g.id
        """,
        (group_id,),
    ).fetchone()
    if row is None:
        raise UserGroupNotFound()
    if not int(row["is_active"]):
        raise UserGroupDisabled()
    return row


def _sync_existing_discord_link(conn: Connection, row, profile: DiscordProfile) -> None:
    old_names = discord_auto_names(
        user_id=profile.user_id,
        username=row["discord_username"],
        global_name=row["discord_global_name"],
    )
    new_name = discord_display_name(profile)
    if row["name"] in old_names and row["name"] != new_name:
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (new_name, int(row["user_id"])))
    conn.execute(
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
