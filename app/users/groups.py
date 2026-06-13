from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import sqlite3

from ..allowlists import AllowedEndpoints, AllowedUpstreams, DEFAULT_ALLOWED_ENDPOINTS
from ..database import Database, utc_now_iso
from ..domain_errors import InvalidDomainInput, UserGroupDisabled, UserGroupNotFound
from ..quota_manager import QuotaManager
from .service import UpdateUserInput

SYNCABLE_MEMBER_FIELDS = {
    "tier",
    "free_small_only",
    "free_small_daily_limit",
    "allowed_endpoints",
    "allowed_upstreams",
    "anlas_quota",
}

PROPAGATE_SCOPE_UNMODIFIED = "unmodified"
PROPAGATE_SCOPE_ALL = "all"
PROPAGATE_SCOPE_NONE = "none"
PROPAGATE_SCOPES = {PROPAGATE_SCOPE_UNMODIFIED, PROPAGATE_SCOPE_ALL, PROPAGATE_SCOPE_NONE}

# 会随组配置覆盖到成员的逻辑字段及展示名，顺序即弹窗展示顺序。
MEMBER_FIELD_LABELS = {
    "tier": "等级",
    "free_small_only": "仅免费小图",
    "free_small_daily_limit": "免费小图单日限制",
    "allowed_endpoints": "允许接口",
    "allowed_upstreams": "允许上游 Key",
    "anlas_quota": "Anlas额度与重置规则",
}


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
            AllowedEndpoints.of(data.default_allowed_endpoints).serialize(),
            AllowedUpstreams.of(data.default_allowed_upstreams).serialize(),
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
        params.append(AllowedEndpoints.of(data.default_allowed_endpoints).serialize())
    if data.default_allowed_upstreams is not None:
        fields.append("default_allowed_upstreams = ?")
        params.append(AllowedUpstreams.of(data.default_allowed_upstreams).serialize())
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
        raise UserGroupNotFound()
    return row


def get_enabled_group(db: Database, group_id: int) -> sqlite3.Row:
    row = get_group(db, group_id)
    if not int(row["is_active"]):
        raise UserGroupDisabled()
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
        "free_small_daily_limit_enabled": bool(row["free_small_daily_limit_enabled"]),
        "free_small_daily_limit": int(row["free_small_daily_limit"] or 0),
        "allowed_endpoints": AllowedEndpoints.parse(row["default_allowed_endpoints"]).as_list(),
        "allowed_upstreams": AllowedUpstreams.parse(row["default_allowed_upstreams"]).as_list(),
        "anlas_total": int(row["default_anlas_total"]),
        "reset_period": str(row["default_reset_period"]),
        "reset_day": int(row["default_reset_day"]),
    }


def apply_group_defaults(row: sqlite3.Row) -> UpdateUserInput:
    defaults = group_defaults(row)
    return UpdateUserInput(
        tier=str(defaults["tier"]),
        free_small_only=bool(defaults["free_small_only"]),
        free_small_daily_limit_enabled=bool(defaults["free_small_daily_limit_enabled"]),
        free_small_daily_limit=int(defaults["free_small_daily_limit"]),
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
        raise InvalidDomainInput(f"Unknown sync field: {', '.join(unknown)}")
    if not selected:
        raise InvalidDomainInput("At least one sync field must be selected")

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
    if "free_small_daily_limit" in selected:
        user_fields.append("free_small_daily_limit_enabled = ?")
        params.append(1 if defaults["free_small_daily_limit_enabled"] else 0)
        user_fields.append("free_small_daily_limit = ?")
        params.append(int(defaults["free_small_daily_limit"]))
    if "allowed_endpoints" in selected:
        user_fields.append("allowed_endpoints = ?")
        params.append(AllowedEndpoints.of(defaults["allowed_endpoints"]).serialize())
    if "allowed_upstreams" in selected:
        user_fields.append("allowed_upstreams = ?")
        params.append(AllowedUpstreams.of(defaults["allowed_upstreams"]).serialize())
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


def preview_group_propagation(db: Database, group_id: int, data: UserGroupUpdateInput) -> dict[str, object]:
    group = get_group(db, group_id)
    old_values = _group_member_values(group)
    new_values = _merged_group_member_values(group, data)
    members = _load_group_members_with_values(db, group_id)
    fields: list[dict[str, object]] = []
    for field in MEMBER_FIELD_LABELS:
        if new_values[field] == old_values[field]:
            continue
        unmodified = sum(1 for _, values in members if values[field] == old_values[field])
        fields.append(
            {
                "field": field,
                "label": MEMBER_FIELD_LABELS[field],
                "old": _display_member_value(field, old_values[field]),
                "new": _display_member_value(field, new_values[field]),
                "unmodified_count": unmodified,
            }
        )
    return {"member_count": len(members), "fields": fields}


def update_group_with_propagation(
    db: Database,
    quota_manager: QuotaManager,
    group_id: int,
    data: UserGroupUpdateInput,
    *,
    propagate_scope: str = PROPAGATE_SCOPE_UNMODIFIED,
) -> dict[str, object]:
    if propagate_scope not in PROPAGATE_SCOPES:
        raise InvalidDomainInput(f"Unknown propagate scope: {propagate_scope}")
    group = get_group(db, group_id)
    old_values = _group_member_values(group)
    new_values = _merged_group_member_values(group, data)
    members = _load_group_members_with_values(db, group_id)
    group_changed = update_group(db, group_id, data)

    changed_fields = [field for field in MEMBER_FIELD_LABELS if new_values[field] != old_values[field]]
    summary: dict[str, object] = {
        "group_changed": group_changed,
        "propagate_scope": propagate_scope,
        "member_count": len(members),
        "updated_users": 0,
        "fields": [],
    }
    if propagate_scope == PROPAGATE_SCOPE_NONE or not changed_fields or not members:
        return summary

    field_counts = {field: 0 for field in changed_fields}
    user_updates: dict[int, dict[str, object]] = {}
    quota_updates: list[int] = []
    for user_id, values in members:
        for field in changed_fields:
            if propagate_scope == PROPAGATE_SCOPE_UNMODIFIED and values[field] != old_values[field]:
                continue
            if values[field] == new_values[field]:
                # 已与新组配置一致的成员无需修改，也不计入覆盖人数。
                continue
            if field == "anlas_quota":
                quota_updates.append(user_id)
            else:
                user_updates.setdefault(user_id, {}).update(_user_columns_for_field(field, new_values[field]))
            field_counts[field] += 1

    if user_updates:
        with db.transaction() as conn:
            for user_id, columns in user_updates.items():
                assignments = ", ".join(f"{column} = ?" for column in columns)
                conn.execute(
                    f"UPDATE users SET {assignments} WHERE id = ? AND deleted_at IS NULL",
                    (*columns.values(), user_id),
                )
    new_total, new_period, new_day = new_values["anlas_quota"]
    for user_id in quota_updates:
        quota_manager.create_or_update(user_id, int(new_total), str(new_period), int(new_day))

    summary["updated_users"] = len(set(user_updates) | set(quota_updates))
    summary["fields"] = [
        {"field": field, "label": MEMBER_FIELD_LABELS[field], "updated": field_counts[field]}
        for field in changed_fields
    ]
    return summary


def _group_member_values(row: sqlite3.Row) -> dict[str, object]:
    return {
        "tier": str(row["default_tier"]),
        "free_small_only": 1 if row["default_free_small_only"] else 0,
        "free_small_daily_limit": (
            1 if row["free_small_daily_limit_enabled"] else 0,
            int(row["free_small_daily_limit"] or 0),
        ),
        "allowed_endpoints": AllowedEndpoints.parse(row["default_allowed_endpoints"]).serialize(),
        "allowed_upstreams": AllowedUpstreams.parse(row["default_allowed_upstreams"]).serialize(),
        "anlas_quota": (
            int(row["default_anlas_total"] or 0),
            str(row["default_reset_period"]),
            int(row["default_reset_day"] or 0),
        ),
    }


def _merged_group_member_values(row: sqlite3.Row, data: UserGroupUpdateInput) -> dict[str, object]:
    current = _group_member_values(row)
    daily_enabled, daily_limit = current["free_small_daily_limit"]
    if data.free_small_daily_limit_enabled is not None:
        daily_enabled = 1 if data.free_small_daily_limit_enabled else 0
    if data.free_small_daily_limit is not None:
        daily_limit = int(data.free_small_daily_limit)
    anlas_total, reset_period, reset_day = current["anlas_quota"]
    if data.default_anlas_total is not None:
        anlas_total = int(data.default_anlas_total)
    if data.default_reset_period is not None:
        reset_period = str(data.default_reset_period)
    if data.default_reset_day is not None:
        reset_day = int(data.default_reset_day)
    return {
        "tier": str(data.default_tier) if data.default_tier is not None else current["tier"],
        "free_small_only": (
            (1 if data.default_free_small_only else 0)
            if data.default_free_small_only is not None
            else current["free_small_only"]
        ),
        "free_small_daily_limit": (daily_enabled, daily_limit),
        "allowed_endpoints": (
            AllowedEndpoints.of(data.default_allowed_endpoints).serialize()
            if data.default_allowed_endpoints is not None
            else current["allowed_endpoints"]
        ),
        "allowed_upstreams": (
            AllowedUpstreams.of(data.default_allowed_upstreams).serialize()
            if data.default_allowed_upstreams is not None
            else current["allowed_upstreams"]
        ),
        "anlas_quota": (anlas_total, reset_period, reset_day),
    }


def _load_group_members_with_values(db: Database, group_id: int) -> list[tuple[int, dict[str, object]]]:
    rows = db.query_all(
        """
        SELECT u.id, u.tier, u.free_small_only,
               u.free_small_daily_limit_enabled, u.free_small_daily_limit,
               u.allowed_endpoints, u.allowed_upstreams,
               q.total AS anlas_total, q.reset_period AS reset_period, q.reset_day AS reset_day
        FROM users u
        LEFT JOIN user_anlas_quota q ON q.user_id = u.id
        WHERE u.group_id = ? AND u.deleted_at IS NULL
        ORDER BY u.id
        """,
        (group_id,),
    )
    return [(int(row["id"]), _user_member_values(row)) for row in rows]


def _user_member_values(row: sqlite3.Row) -> dict[str, object]:
    return {
        "tier": str(row["tier"]),
        "free_small_only": 1 if row["free_small_only"] else 0,
        "free_small_daily_limit": (
            1 if row["free_small_daily_limit_enabled"] else 0,
            int(row["free_small_daily_limit"] or 0),
        ),
        "allowed_endpoints": AllowedEndpoints.parse(row["allowed_endpoints"]).serialize(),
        "allowed_upstreams": AllowedUpstreams.parse(row["allowed_upstreams"]).serialize(),
        "anlas_quota": (
            int(row["anlas_total"] or 0),
            str(row["reset_period"] or "month"),
            int(row["reset_day"] if row["reset_day"] is not None else 0),
        ),
    }


def _user_columns_for_field(field: str, value: object) -> dict[str, object]:
    if field == "tier":
        return {"tier": str(value)}
    if field == "free_small_only":
        return {"free_small_only": 1 if value else 0}
    if field == "free_small_daily_limit":
        enabled, limit = value
        return {"free_small_daily_limit_enabled": 1 if enabled else 0, "free_small_daily_limit": int(limit)}
    if field == "allowed_endpoints":
        return {"allowed_endpoints": str(value)}
    if field == "allowed_upstreams":
        return {"allowed_upstreams": value if value is None else str(value)}
    raise ValueError(f"Unsupported member field: {field}")


def _display_member_value(field: str, value: object) -> str:
    if field == "free_small_only":
        return "开启" if value else "关闭"
    if field == "free_small_daily_limit":
        enabled, limit = value
        return f"启用（每日 {limit} 张）" if enabled else "关闭"
    if field == "allowed_upstreams":
        return str(value) if value else "全部上游"
    if field == "anlas_quota":
        total, period, day = value
        return f"总额 {total} · 周期 {period} · 重置日 {day}"
    return str(value)
