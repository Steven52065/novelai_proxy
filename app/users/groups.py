from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import sqlite3
from fastapi import HTTPException

from ..database import Database, utc_now_iso
from ..quota_manager import QuotaManager
from .service import (
    DEFAULT_ALLOWED_ENDPOINTS,
    UpdateUserInput,
    parse_allowed_endpoints,
    parse_allowed_upstreams,
    serialize_allowed_endpoints,
    serialize_allowed_upstreams,
)

SYNCABLE_MEMBER_FIELDS = {"tier", "free_small_only", "allowed_endpoints", "allowed_upstreams", "anlas_quota"}


@dataclass(frozen=True)
class UserGroupInput:
    name: str
    is_active: bool = True
    default_tier: str = "normal"
    default_free_small_only: bool = True
    free_small_daily_limit_enabled: bool = False
    free_small_daily_limit: int = 0
    default_allowed_endpoints: list[str] = field(default_factory=lambda: [DEFAULT_ALLOWED_ENDPOINTS])
    default_allowed_upstreams: list[str] = field(default_factory=list)
    default_anlas_total: int = 0
    default_reset_period: str = "month"
    default_reset_day: int = 1


@dataclass(frozen=True)
class UserGroupUpdateInput:
    name: str | None = None
    is_active: bool | None = None
    default_tier: str | None = None
    default_free_small_only: bool | None = None
    free_small_daily_limit_enabled: bool | None = None
    free_small_daily_limit: int | None = None
    default_allowed_endpoints: list[str] | None = None
    default_allowed_upstreams: list[str] | None = None
    default_anlas_total: int | None = None
    default_reset_period: str | None = None
    default_reset_day: int | None = None


def create_group(db: Database, data: UserGroupInput) -> int:
    now = utc_now_iso()
    cursor = db.execute(
        """
        INSERT INTO user_groups (
            name, is_active, default_tier, default_free_small_only,
            free_small_daily_limit_enabled, free_small_daily_limit,
            default_allowed_endpoints, default_allowed_upstreams, default_anlas_total,
            default_reset_period, default_reset_day, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data.name,
            1 if data.is_active else 0,
            data.default_tier,
            1 if data.default_free_small_only else 0,
            1 if data.free_small_daily_limit_enabled else 0,
            data.free_small_daily_limit,
            serialize_allowed_endpoints(data.default_allowed_endpoints),
            serialize_allowed_upstreams(data.default_allowed_upstreams),
            data.default_anlas_total,
            data.default_reset_period,
            data.default_reset_day,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def update_group(db: Database, group_id: int, data: UserGroupUpdateInput) -> bool:
    ensure_group_exists(db, group_id)
    fields: list[str] = []
    params: list[object] = []
    if data.name is not None:
        fields.append("name = ?")
        params.append(data.name)
    if data.is_active is not None:
        fields.append("is_active = ?")
        params.append(1 if data.is_active else 0)
    if data.default_tier is not None:
        fields.append("default_tier = ?")
        params.append(data.default_tier)
    if data.default_free_small_only is not None:
        fields.append("default_free_small_only = ?")
        params.append(1 if data.default_free_small_only else 0)
    if data.free_small_daily_limit_enabled is not None:
        fields.append("free_small_daily_limit_enabled = ?")
        params.append(1 if data.free_small_daily_limit_enabled else 0)
    if data.free_small_daily_limit is not None:
        fields.append("free_small_daily_limit = ?")
        params.append(data.free_small_daily_limit)
    if data.default_allowed_endpoints is not None:
        fields.append("default_allowed_endpoints = ?")
        params.append(serialize_allowed_endpoints(data.default_allowed_endpoints))
    if data.default_allowed_upstreams is not None:
        fields.append("default_allowed_upstreams = ?")
        params.append(serialize_allowed_upstreams(data.default_allowed_upstreams))
    if data.default_anlas_total is not None:
        fields.append("default_anlas_total = ?")
        params.append(data.default_anlas_total)
    if data.default_reset_period is not None:
        fields.append("default_reset_period = ?")
        params.append(data.default_reset_period)
    if data.default_reset_day is not None:
        fields.append("default_reset_day = ?")
        params.append(data.default_reset_day)
    if not fields:
        return False
    fields.append("updated_at = ?")
    params.append(utc_now_iso())
    params.append(group_id)
    db.execute(f"UPDATE user_groups SET {', '.join(fields)} WHERE id = ?", tuple(params))
    return True


def delete_or_disable_group(db: Database, group_id: int) -> None:
    ensure_group_exists(db, group_id)
    db.execute(
        "UPDATE user_groups SET is_active = 0, updated_at = ? WHERE id = ?",
        (utc_now_iso(), group_id),
    )


def get_group(db: Database, group_id: int) -> sqlite3.Row:
    row = db.query_one(
        """
        SELECT g.*,
               COUNT(u.id) AS member_count
        FROM user_groups g
        LEFT JOIN users u ON u.group_id = g.id AND u.deleted_at IS NULL
        WHERE g.id = ?
        GROUP BY g.id
        """,
        (group_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail={"message": "User group not found"})
    return row


def get_enabled_group(db: Database, group_id: int) -> sqlite3.Row:
    row = get_group(db, group_id)
    if not int(row["is_active"]):
        raise HTTPException(status_code=400, detail={"message": "User group is disabled"})
    return row


def ensure_group_exists(db: Database, group_id: int) -> None:
    get_group(db, group_id)


def list_groups(db: Database) -> list[sqlite3.Row]:
    return db.query_all(
        """
        SELECT g.*,
               COUNT(u.id) AS member_count
        FROM user_groups g
        LEFT JOIN users u ON u.group_id = g.id AND u.deleted_at IS NULL
        GROUP BY g.id
        ORDER BY g.id DESC
        """
    )


def group_defaults(row: sqlite3.Row) -> dict[str, object]:
    return {
        "tier": str(row["default_tier"]),
        "free_small_only": bool(row["default_free_small_only"]),
        "allowed_endpoints": parse_allowed_endpoints(row["default_allowed_endpoints"]),
        "allowed_upstreams": parse_allowed_upstreams(row["default_allowed_upstreams"]),
        "anlas_total": int(row["default_anlas_total"]),
        "reset_period": str(row["default_reset_period"]),
        "reset_day": int(row["default_reset_day"]),
    }


def apply_group_defaults(row: sqlite3.Row) -> UpdateUserInput:
    defaults = group_defaults(row)
    return UpdateUserInput(
        tier=str(defaults["tier"]),
        free_small_only=bool(defaults["free_small_only"]),
        allowed_endpoints=list(defaults["allowed_endpoints"]),
        allowed_upstreams=list(defaults["allowed_upstreams"]),
        anlas_total=int(defaults["anlas_total"]),
        reset_period=str(defaults["reset_period"]),
        reset_day=int(defaults["reset_day"]),
    )


def sync_group_members(
    db: Database,
    quota_manager: QuotaManager,
    group_id: int,
    fields: Iterable[str],
) -> int:
    group = get_group(db, group_id)
    selected = set(fields)
    unknown = sorted(selected - SYNCABLE_MEMBER_FIELDS)
    if unknown:
        raise HTTPException(status_code=400, detail={"message": f"Unknown sync field: {', '.join(unknown)}"})
    if not selected:
        raise HTTPException(status_code=400, detail={"message": "At least one sync field must be selected"})

    defaults = group_defaults(group)
    members = db.query_all(
        "SELECT id FROM users WHERE group_id = ? AND deleted_at IS NULL ORDER BY id",
        (group_id,),
    )

    user_fields: list[str] = []
    params: list[object] = []
    if "tier" in selected:
        user_fields.append("tier = ?")
        params.append(defaults["tier"])
    if "free_small_only" in selected:
        user_fields.append("free_small_only = ?")
        params.append(1 if defaults["free_small_only"] else 0)
    if "allowed_endpoints" in selected:
        user_fields.append("allowed_endpoints = ?")
        params.append(serialize_allowed_endpoints(list(defaults["allowed_endpoints"])))
    if "allowed_upstreams" in selected:
        user_fields.append("allowed_upstreams = ?")
        params.append(serialize_allowed_upstreams(list(defaults["allowed_upstreams"])))
    if user_fields:
        params.append(group_id)
        db.execute(
            f"UPDATE users SET {', '.join(user_fields)} WHERE group_id = ? AND deleted_at IS NULL",
            tuple(params),
        )

    if "anlas_quota" in selected:
        for member in members:
            quota_manager.create_or_update(
                int(member["id"]),
                int(defaults["anlas_total"]),
                str(defaults["reset_period"]),
                int(defaults["reset_day"]),
            )
    return len(members)
