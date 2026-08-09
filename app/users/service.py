from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from sqlite3 import Connection

from ..allowlists import AllowedEndpoints, AllowedUpstreams, DEFAULT_ALLOWED_ENDPOINTS
from ..database import Database, utc_now_iso
from ..domain_errors import InvalidDomainInput, UserNotFound
from ..image_format_policies import DEFAULT_IMAGE_FORMAT_POLICY, ImageFormatPolicy, normalize_image_format_policy
from ..quota_manager import QuotaManager
from ..queue_tiers import TIER_NORMAL
from ..rate_limit_rules import RateLimitRuleSet
from ..security import generate_api_key, hash_api_key


@dataclass(frozen=True)
class CreateUserInput:
    name: str
    tier: str = TIER_NORMAL
    free_small_only: bool = False
    free_small_daily_limit_enabled: bool = False
    free_small_daily_limit: int = 0
    allowed_endpoints: list[str] = field(default_factory=lambda: [DEFAULT_ALLOWED_ENDPOINTS])
    allowed_upstreams: list[str] = field(default_factory=list)
    image_format_policy: ImageFormatPolicy = DEFAULT_IMAGE_FORMAT_POLICY
    group_id: int | None = None
    anlas_total: int = 0
    reset_period: str = "month"
    reset_day: int | None = None
    # None 表示不写入限频规则；空规则集表示明确不限频。
    rate_limit_rules: RateLimitRuleSet | None = None


@dataclass(frozen=True)
class UpdateUserInput:
    name: str | None = None
    tier: str | None = None
    is_active: bool | None = None
    free_small_only: bool | None = None
    free_small_daily_limit_enabled: bool | None = None
    free_small_daily_limit: int | None = None
    allowed_endpoints: list[str] | None = None
    allowed_upstreams: list[str] | None = None
    image_format_policy: ImageFormatPolicy | None = None
    group_id: int | None = None
    update_group_id: bool = False
    anlas_total: int | None = None
    reset_period: str | None = None
    reset_day: int | None = None
    # None 表示不修改；空规则集表示清空该用户的限频规则。
    rate_limit_rules: RateLimitRuleSet | None = None


def replace_user_rate_limit_rules_with_connection(
    conn: Connection,
    user_id: int,
    rules: RateLimitRuleSet,
    *,
    now: str | None = None,
) -> None:
    """整体替换用户的限频规则：先清空再写入，与组模板的覆盖语义一致。"""
    created_at = now or utc_now_iso()
    conn.execute("DELETE FROM rate_limit_rules WHERE user_id = ?", (user_id,))
    for rule in rules.rules:
        conn.execute(
            """
            INSERT INTO rate_limit_rules (user_id, period, max_requests, is_active, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, rule.period, rule.max_requests, 1 if rule.is_active else 0, created_at),
        )


def load_user_rate_limit_rules(db: Database, user_id: int) -> RateLimitRuleSet:
    return RateLimitRuleSet.from_rows(
        db.query_all(
            """
            SELECT period, max_requests, is_active
            FROM rate_limit_rules
            WHERE user_id = ?
            ORDER BY id
            """,
            (user_id,),
        )
    )


@dataclass(frozen=True)
class CreatedUser:
    user_id: int
    api_key: str


def insert_user_record(
    conn: Connection,
    data: CreateUserInput,
    *,
    api_key: str | None = None,
    now: str | None = None,
) -> CreatedUser:
    api_key = api_key or generate_api_key()
    now = now or utc_now_iso()
    cursor = conn.execute(
        """
        INSERT INTO users (
            api_key_hash, name, tier, is_active, free_small_only,
            free_small_daily_limit_enabled, free_small_daily_limit,
            allowed_endpoints, allowed_upstreams, image_format_policy, group_id, created_at
        )
        VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            hash_api_key(api_key),
            data.name,
            data.tier,
            1 if data.free_small_only else 0,
            1 if data.free_small_daily_limit_enabled else 0,
            data.free_small_daily_limit,
            AllowedEndpoints.of(data.allowed_endpoints).serialize(),
            AllowedUpstreams.of(data.allowed_upstreams).serialize(),
            normalize_image_format_policy(data.image_format_policy),
            data.group_id,
            now,
        ),
    )
    created = CreatedUser(user_id=int(cursor.lastrowid), api_key=api_key)
    if data.rate_limit_rules is not None:
        replace_user_rate_limit_rules_with_connection(conn, created.user_id, data.rate_limit_rules, now=now)
    return created


def create_user(db: Database, quota_manager: QuotaManager, data: CreateUserInput) -> CreatedUser:
    now = utc_now_iso()
    try:
        with db.transaction() as conn:
            created = insert_user_record(conn, data, now=now)
            quota_manager.create_or_update_with_connection(
                conn,
                created.user_id,
                data.anlas_total,
                data.reset_period,
                data.reset_day,
                now=now,
            )
    except ValueError as exc:
        raise InvalidDomainInput(str(exc)) from exc
    return created


def update_user(db: Database, quota_manager: QuotaManager, user_id: int, data: UpdateUserInput) -> bool:
    ensure_user_exists(db, user_id)
    fields: list[str] = []
    params: list[object] = []
    if data.name is not None:
        fields.append("name = ?")
        params.append(data.name)
    if data.tier is not None:
        fields.append("tier = ?")
        params.append(data.tier)
    if data.is_active is not None:
        fields.append("is_active = ?")
        params.append(1 if data.is_active else 0)
    if data.free_small_only is not None:
        fields.append("free_small_only = ?")
        params.append(1 if data.free_small_only else 0)
    if data.free_small_daily_limit_enabled is not None:
        fields.append("free_small_daily_limit_enabled = ?")
        params.append(1 if data.free_small_daily_limit_enabled else 0)
    if data.free_small_daily_limit is not None:
        fields.append("free_small_daily_limit = ?")
        params.append(data.free_small_daily_limit)
    if data.allowed_endpoints is not None:
        fields.append("allowed_endpoints = ?")
        params.append(AllowedEndpoints.of(data.allowed_endpoints).serialize())
    if data.allowed_upstreams is not None:
        fields.append("allowed_upstreams = ?")
        params.append(AllowedUpstreams.of(data.allowed_upstreams).serialize())
    if data.image_format_policy is not None:
        fields.append("image_format_policy = ?")
        params.append(normalize_image_format_policy(data.image_format_policy))
    if data.update_group_id:
        fields.append("group_id = ?")
        params.append(data.group_id)
    if fields:
        params.append(user_id)
        db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", tuple(params))

    rules_changed = False
    if data.rate_limit_rules is not None and load_user_rate_limit_rules(db, user_id) != data.rate_limit_rules:
        with db.transaction() as conn:
            replace_user_rate_limit_rules_with_connection(conn, user_id, data.rate_limit_rules)
        rules_changed = True

    has_quota_update = data.anlas_total is not None or data.reset_period is not None or data.reset_day is not None
    if has_quota_update:
        quota = db.query_one(
            "SELECT total, reset_period, reset_day FROM user_anlas_quota WHERE user_id = ?",
            (user_id,),
        )
        total = data.anlas_total if data.anlas_total is not None else int(quota["total"]) if quota else 0
        reset_period = data.reset_period if data.reset_period is not None else str(quota["reset_period"]) if quota else "month"
        if data.reset_day is not None:
            reset_day = data.reset_day
        elif data.reset_period is not None:
            reset_day = None
        else:
            reset_day = int(quota["reset_day"]) if quota else None
        try:
            quota_manager.create_or_update(user_id, total, reset_period, reset_day)
        except ValueError as exc:
            raise InvalidDomainInput(str(exc)) from exc
    return bool(fields or has_quota_update or rules_changed)


def delete_user(db: Database, user_id: int) -> None:
    ensure_user_exists(db, user_id)
    deleted_hash = f"deleted:{user_id}:{secrets.token_urlsafe(24)}"
    db.execute(
        """
        UPDATE users
        SET is_active = 0,
            deleted_at = ?,
            api_key_hash = ?,
            api_key = NULL
        WHERE id = ? AND deleted_at IS NULL
        """,
        (utc_now_iso(), deleted_hash, user_id),
    )


def reset_api_key(db: Database, user_id: int) -> str:
    ensure_user_exists(db, user_id)
    api_key = generate_api_key()
    db.execute(
        "UPDATE users SET api_key_hash = ?, api_key = NULL WHERE id = ?",
        (hash_api_key(api_key), user_id),
    )
    return api_key


def ensure_user_exists(db: Database, user_id: int) -> None:
    row = db.query_one(
        "SELECT id FROM users WHERE id = ? AND deleted_at IS NULL",
        (user_id,),
    )
    if row is None:
        raise UserNotFound()


def user_is_available(db: Database, user_id: int) -> bool:
    row = db.query_one(
        "SELECT is_active, deleted_at FROM users WHERE id = ?",
        (user_id,),
    )
    return row is not None and bool(row["is_active"]) and row["deleted_at"] is None
