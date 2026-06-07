from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from sqlite3 import Connection

from fastapi import HTTPException

from ..database import Database, utc_now_iso
from ..quota_manager import QuotaManager
from ..security import generate_api_key, hash_api_key

DEFAULT_ALLOWED_ENDPOINTS = "generate-image"


@dataclass(frozen=True)
class CreateUserInput:
    name: str
    tier: str = "normal"
    free_small_only: bool = False
    allowed_endpoints: list[str] = field(default_factory=lambda: [DEFAULT_ALLOWED_ENDPOINTS])
    allowed_upstreams: list[str] = field(default_factory=list)
    group_id: int | None = None
    anlas_total: int = 0
    reset_period: str = "month"
    reset_day: int | None = None


@dataclass(frozen=True)
class UpdateUserInput:
    name: str | None = None
    tier: str | None = None
    is_active: bool | None = None
    free_small_only: bool | None = None
    allowed_endpoints: list[str] | None = None
    allowed_upstreams: list[str] | None = None
    group_id: int | None = None
    update_group_id: bool = False
    anlas_total: int | None = None
    reset_period: str | None = None
    reset_day: int | None = None


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
            allowed_endpoints, allowed_upstreams, group_id, created_at
        )
        VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            hash_api_key(api_key),
            data.name,
            data.tier,
            1 if data.free_small_only else 0,
            serialize_allowed_endpoints(data.allowed_endpoints),
            serialize_allowed_upstreams(data.allowed_upstreams),
            data.group_id,
            now,
        ),
    )
    return CreatedUser(user_id=int(cursor.lastrowid), api_key=api_key)


def create_user(db: Database, quota_manager: QuotaManager, data: CreateUserInput) -> CreatedUser:
    now = utc_now_iso()
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
    if data.allowed_endpoints is not None:
        fields.append("allowed_endpoints = ?")
        params.append(serialize_allowed_endpoints(data.allowed_endpoints))
    if data.allowed_upstreams is not None:
        fields.append("allowed_upstreams = ?")
        params.append(serialize_allowed_upstreams(data.allowed_upstreams))
    if data.update_group_id:
        fields.append("group_id = ?")
        params.append(data.group_id)
    if fields:
        params.append(user_id)
        db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", tuple(params))

    has_quota_update = data.anlas_total is not None or data.reset_period is not None or data.reset_day is not None
    if has_quota_update:
        quota = db.query_one(
            "SELECT total, reset_period, reset_day FROM user_anlas_quota WHERE user_id = ?",
            (user_id,),
        )
        total = data.anlas_total if data.anlas_total is not None else int(quota["total"]) if quota else 0
        reset_period = data.reset_period if data.reset_period is not None else str(quota["reset_period"]) if quota else "month"
        reset_day = data.reset_day if data.reset_day is not None else int(quota["reset_day"]) if quota else None
        quota_manager.create_or_update(user_id, total, reset_period, reset_day)
    return bool(fields or has_quota_update)


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
        raise HTTPException(status_code=404, detail={"message": "User not found"})


def serialize_allowed_endpoints(value: list[str] | None) -> str:
    if not value:
        return DEFAULT_ALLOWED_ENDPOINTS
    valid: list[str] = []
    for endpoint in value:
        endpoint = endpoint.strip()
        if endpoint and endpoint not in valid:
            valid.append(endpoint)
    return ",".join(valid or [DEFAULT_ALLOWED_ENDPOINTS])


def serialize_allowed_upstreams(value: list[str] | None) -> str | None:
    if not value:
        return None
    valid: list[str] = []
    for upstream_id in value:
        upstream_id = upstream_id.strip()
        if upstream_id and upstream_id not in valid:
            valid.append(upstream_id)
    return ",".join(valid) if valid else None


def parse_allowed_endpoints(value: str | None) -> list[str]:
    if not value:
        return [DEFAULT_ALLOWED_ENDPOINTS]
    endpoints = [item.strip() for item in value.split(",") if item.strip()]
    return endpoints or [DEFAULT_ALLOWED_ENDPOINTS]


def parse_allowed_upstreams(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]
