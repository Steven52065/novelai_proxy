from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

import sqlite3

from ..allowlists import AllowedEndpoints, AllowedUpstreams, DEFAULT_ALLOWED_ENDPOINTS
from ..database import Database, utc_now_iso
from ..domain_errors import InvalidDomainInput, UserGroupDisabled, UserGroupNotFound
from ..image_format_policies import (
    DEFAULT_IMAGE_FORMAT_POLICY,
    ImageFormatPolicy,
    image_format_policy_label,
    normalize_image_format_policy,
)
from ..quota_manager import QuotaManager
from ..queue_tiers import TIER_NORMAL
from ..rate_limit_rules import EMPTY_RATE_LIMIT_RULES, RateLimitRuleSet
from .service import UpdateUserInput, replace_user_rate_limit_rules_with_connection

PROPAGATE_SCOPE_UNMODIFIED = "unmodified"
PROPAGATE_SCOPE_ALL = "all"
PROPAGATE_SCOPE_NONE = "none"
PROPAGATE_SCOPES = {PROPAGATE_SCOPE_UNMODIFIED, PROPAGATE_SCOPE_ALL, PROPAGATE_SCOPE_NONE}

# 成员字段的写入方式：写 users 表的列、写 anlas 额度、或替换用户的限频规则。
WRITER_COLUMNS = "columns"
WRITER_QUOTA = "quota"
WRITER_RATE_LIMIT_RULES = "rate_limit_rules"


@dataclass(frozen=True)
class UserGroupInput:
    name: str
    is_active: bool = True
    default_tier: str = TIER_NORMAL
    default_free_small_only: bool = True
    free_small_daily_limit_enabled: bool = False
    free_small_daily_limit: int = 0
    default_allowed_endpoints: list[str] = field(default_factory=lambda: [DEFAULT_ALLOWED_ENDPOINTS])
    default_allowed_upstreams: list[str] = field(default_factory=list)
    default_image_format_policy: ImageFormatPolicy = DEFAULT_IMAGE_FORMAT_POLICY
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
    default_image_format_policy: ImageFormatPolicy | None = None
    default_anlas_total: int | None = None
    default_reset_period: str | None = None
    default_reset_day: int | None = None
    # None 表示不修改；空规则集表示清空组模板。
    member_rate_limit_rules: RateLimitRuleSet | None = None


@dataclass(frozen=True)
class MemberFieldSpec:
    name: str
    label: str
    group_value: Callable[[Mapping[str, object]], object]
    merged_value: Callable[[object, UserGroupUpdateInput], object]
    user_value: Callable[[Mapping[str, object]], object]
    user_columns: Callable[[object], dict[str, object]] | None
    display_value: Callable[[object], str] = str
    writer: str = WRITER_COLUMNS


def _merge_daily_limit(current: object, data: UserGroupUpdateInput) -> tuple[int, int]:
    enabled, limit = current
    if data.free_small_daily_limit_enabled is not None:
        enabled = 1 if data.free_small_daily_limit_enabled else 0
    if data.free_small_daily_limit is not None:
        limit = int(data.free_small_daily_limit)
    return int(enabled), int(limit)


def _merge_anlas_quota(current: object, data: UserGroupUpdateInput) -> tuple[int, str, int]:
    total, period, day = current
    if data.default_anlas_total is not None:
        total = int(data.default_anlas_total)
    if data.default_reset_period is not None:
        period = str(data.default_reset_period)
    if data.default_reset_day is not None:
        day = int(data.default_reset_day)
    return int(total), str(period), int(day)


def _display_free_small_only(value: object) -> str:
    return "开启" if value else "关闭"


def _display_daily_limit(value: object) -> str:
    enabled, limit = value
    return f"启用（每日 {limit} 张）" if enabled else "关闭"


def _display_allowed_upstreams(value: object) -> str:
    return str(value) if value else "全部上游"


def _display_anlas_quota(value: object) -> str:
    total, period, day = value
    return f"总额 {total} · 周期 {period} · 重置日 {day}"


# 会随组配置覆盖到成员的逻辑字段。顺序即弹窗展示和同步更新顺序。
MEMBER_FIELD_SPECS = (
    MemberFieldSpec(
        name="tier",
        label="等级",
        group_value=lambda row: str(row["default_tier"]),
        merged_value=lambda current, data: str(data.default_tier) if data.default_tier is not None else current,
        user_value=lambda row: str(row["tier"]),
        user_columns=lambda value: {"tier": str(value)},
    ),
    MemberFieldSpec(
        name="free_small_only",
        label="仅免费小图",
        group_value=lambda row: 1 if row["default_free_small_only"] else 0,
        merged_value=lambda current, data: (
            (1 if data.default_free_small_only else 0)
            if data.default_free_small_only is not None
            else current
        ),
        user_value=lambda row: 1 if row["free_small_only"] else 0,
        user_columns=lambda value: {"free_small_only": 1 if value else 0},
        display_value=_display_free_small_only,
    ),
    MemberFieldSpec(
        name="free_small_daily_limit",
        label="免费小图单日限制",
        group_value=lambda row: (
            1 if row["free_small_daily_limit_enabled"] else 0,
            int(row["free_small_daily_limit"] or 0),
        ),
        merged_value=_merge_daily_limit,
        user_value=lambda row: (
            1 if row["free_small_daily_limit_enabled"] else 0,
            int(row["free_small_daily_limit"] or 0),
        ),
        user_columns=lambda value: {
            "free_small_daily_limit_enabled": 1 if value[0] else 0,
            "free_small_daily_limit": int(value[1]),
        },
        display_value=_display_daily_limit,
    ),
    MemberFieldSpec(
        name="member_rate_limit_rules",
        label="用户独享限流",
        group_value=lambda row: row["member_rate_limit_rules"],
        merged_value=lambda current, data: (
            data.member_rate_limit_rules if data.member_rate_limit_rules is not None else current
        ),
        user_value=lambda row: row["member_rate_limit_rules"],
        user_columns=None,
        display_value=lambda value: value.display(),
        writer=WRITER_RATE_LIMIT_RULES,
    ),
    MemberFieldSpec(
        name="allowed_endpoints",
        label="允许接口",
        group_value=lambda row: AllowedEndpoints.parse(row["default_allowed_endpoints"]).serialize(),
        merged_value=lambda current, data: (
            AllowedEndpoints.of(data.default_allowed_endpoints).serialize()
            if data.default_allowed_endpoints is not None
            else current
        ),
        user_value=lambda row: AllowedEndpoints.parse(row["allowed_endpoints"]).serialize(),
        user_columns=lambda value: {"allowed_endpoints": str(value)},
    ),
    MemberFieldSpec(
        name="allowed_upstreams",
        label="允许上游 Key",
        group_value=lambda row: AllowedUpstreams.parse(row["default_allowed_upstreams"]).serialize(),
        merged_value=lambda current, data: (
            AllowedUpstreams.of(data.default_allowed_upstreams).serialize()
            if data.default_allowed_upstreams is not None
            else current
        ),
        user_value=lambda row: AllowedUpstreams.parse(row["allowed_upstreams"]).serialize(),
        user_columns=lambda value: {"allowed_upstreams": value if value is None else str(value)},
        display_value=_display_allowed_upstreams,
    ),
    MemberFieldSpec(
        name="image_format_policy",
        label="图片格式策略",
        group_value=lambda row: normalize_image_format_policy(row["default_image_format_policy"]),
        merged_value=lambda current, data: (
            normalize_image_format_policy(data.default_image_format_policy)
            if data.default_image_format_policy is not None
            else current
        ),
        user_value=lambda row: normalize_image_format_policy(row["image_format_policy"]),
        user_columns=lambda value: {"image_format_policy": normalize_image_format_policy(value)},
        display_value=image_format_policy_label,
    ),
    MemberFieldSpec(
        name="anlas_quota",
        label="Anlas额度与重置规则",
        group_value=lambda row: (
            int(row["default_anlas_total"] or 0),
            str(row["default_reset_period"]),
            int(row["default_reset_day"] or 0),
        ),
        merged_value=_merge_anlas_quota,
        user_value=lambda row: (
            int(row["anlas_total"] or 0),
            str(row["reset_period"] or "month"),
            int(row["reset_day"] if row["reset_day"] is not None else 0),
        ),
        user_columns=None,
        display_value=_display_anlas_quota,
        writer=WRITER_QUOTA,
    ),
)
MEMBER_FIELD_LABELS = {spec.name: spec.label for spec in MEMBER_FIELD_SPECS}
SYNCABLE_MEMBER_FIELDS = frozenset(MEMBER_FIELD_LABELS)
_MEMBER_FIELD_SPECS_BY_NAME = {spec.name: spec for spec in MEMBER_FIELD_SPECS}


def create_group(db: Database, data: UserGroupInput) -> int:
    now = utc_now_iso()
    cursor = db.execute(
        """
        INSERT INTO user_groups (
            name, is_active, default_tier, default_free_small_only,
            free_small_daily_limit_enabled, free_small_daily_limit,
            default_allowed_endpoints, default_allowed_upstreams, default_image_format_policy, default_anlas_total,
            default_reset_period, default_reset_day, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            normalize_image_format_policy(data.default_image_format_policy),
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
    if data.default_image_format_policy is not None:
        fields.append("default_image_format_policy = ?")
        params.append(normalize_image_format_policy(data.default_image_format_policy))
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


def load_group_member_rate_limit_rules(db: Database, group_id: int) -> RateLimitRuleSet:
    return RateLimitRuleSet.from_rows(
        db.query_all(
            """
            SELECT period, max_requests, is_active
            FROM group_member_rate_limit_rules
            WHERE group_id = ?
            ORDER BY id
            """,
            (group_id,),
        )
    )


def load_group_member_rate_limit_rules_with_connection(conn: sqlite3.Connection, group_id: int) -> RateLimitRuleSet:
    return RateLimitRuleSet.from_rows(
        conn.execute(
            """
            SELECT period, max_requests, is_active
            FROM group_member_rate_limit_rules
            WHERE group_id = ?
            ORDER BY id
            """,
            (group_id,),
        ).fetchall()
    )


def replace_group_member_rate_limit_rules(db: Database, group_id: int, rules: RateLimitRuleSet) -> bool:
    if load_group_member_rate_limit_rules(db, group_id) == rules:
        return False
    now = utc_now_iso()
    with db.transaction() as conn:
        conn.execute("DELETE FROM group_member_rate_limit_rules WHERE group_id = ?", (group_id,))
        for rule in rules.rules:
            conn.execute(
                """
                INSERT INTO group_member_rate_limit_rules (group_id, period, max_requests, is_active, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (group_id, rule.period, rule.max_requests, 1 if rule.is_active else 0, now),
            )
    return True


def group_defaults(row: sqlite3.Row) -> dict[str, object]:
    return {
        "tier": str(row["default_tier"]),
        "free_small_only": bool(row["default_free_small_only"]),
        "free_small_daily_limit_enabled": bool(row["free_small_daily_limit_enabled"]),
        "free_small_daily_limit": int(row["free_small_daily_limit"] or 0),
        "allowed_endpoints": AllowedEndpoints.parse(row["default_allowed_endpoints"]).as_list(),
        "allowed_upstreams": AllowedUpstreams.parse(row["default_allowed_upstreams"]).as_list(),
        "image_format_policy": normalize_image_format_policy(row["default_image_format_policy"]),
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
        image_format_policy=normalize_image_format_policy(defaults["image_format_policy"]),
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

    members = _load_group_members_with_values(db, group_id)
    selected_fields = _ordered_member_fields(selected)
    updated_users, _ = _apply_member_field_updates(
        db,
        quota_manager,
        members,
        selected_fields,
        _group_member_values(group, load_group_member_rate_limit_rules(db, group_id)),
        skip_unchanged=False,
    )
    return len(updated_users)


def preview_group_propagation(db: Database, group_id: int, data: UserGroupUpdateInput) -> dict[str, object]:
    group = get_group(db, group_id)
    group_rules = load_group_member_rate_limit_rules(db, group_id)
    old_values = _group_member_values(group, group_rules)
    new_values = _merged_group_member_values(group, group_rules, data)
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
    # 组模板与成员现值都必须在任何写入之前读取，否则跟随判定会拿到新值。
    group_rules = load_group_member_rate_limit_rules(db, group_id)
    old_values = _group_member_values(group, group_rules)
    new_values = _merged_group_member_values(group, group_rules, data)
    members = _load_group_members_with_values(db, group_id)
    group_changed = update_group(db, group_id, data)
    if data.member_rate_limit_rules is not None:
        rules_changed = replace_group_member_rate_limit_rules(db, group_id, data.member_rate_limit_rules)
        group_changed = group_changed or rules_changed

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

    def follows_group(field_name: str, values: dict[str, object]) -> bool:
        return propagate_scope != PROPAGATE_SCOPE_UNMODIFIED or values[field_name] == old_values[field_name]

    updated_users, field_counts = _apply_member_field_updates(
        db,
        quota_manager,
        members,
        changed_fields,
        new_values,
        should_update=follows_group,
        skip_unchanged=True,
    )

    summary["updated_users"] = len(updated_users)
    summary["fields"] = [
        {"field": field, "label": MEMBER_FIELD_LABELS[field], "updated": field_counts[field]}
        for field in changed_fields
    ]
    return summary


def _group_member_values(row: sqlite3.Row, rules: RateLimitRuleSet) -> dict[str, object]:
    source = _with_member_rate_limit_rules(row, rules)
    return {spec.name: spec.group_value(source) for spec in MEMBER_FIELD_SPECS}


def _merged_group_member_values(
    row: sqlite3.Row,
    rules: RateLimitRuleSet,
    data: UserGroupUpdateInput,
) -> dict[str, object]:
    current = _group_member_values(row, rules)
    return {spec.name: spec.merged_value(current[spec.name], data) for spec in MEMBER_FIELD_SPECS}


def _with_member_rate_limit_rules(row: sqlite3.Row, rules: RateLimitRuleSet) -> dict[str, object]:
    # 限频规则存在独立表里，取不到列，取值前先并进 dict。dict 与 sqlite3.Row
    # 都支持下标访问，因此其余字段的取值函数无需区分来源。
    values = dict(row)
    values["member_rate_limit_rules"] = rules
    return values


def _load_group_members_with_values(db: Database, group_id: int) -> list[tuple[int, dict[str, object]]]:
    rows = db.query_all(
        """
        SELECT u.id, u.tier, u.free_small_only,
               u.free_small_daily_limit_enabled, u.free_small_daily_limit,
               u.allowed_endpoints, u.allowed_upstreams, u.image_format_policy,
               q.total AS anlas_total, q.reset_period AS reset_period, q.reset_day AS reset_day
        FROM users u
        LEFT JOIN user_anlas_quota q ON q.user_id = u.id
        WHERE u.group_id = ? AND u.deleted_at IS NULL
        ORDER BY u.id
        """,
        (group_id,),
    )
    rules_by_user = _load_member_rate_limit_rules(db, group_id)
    return [
        (
            int(row["id"]),
            _user_member_values(row, rules_by_user.get(int(row["id"]), EMPTY_RATE_LIMIT_RULES)),
        )
        for row in rows
    ]


def _load_member_rate_limit_rules(db: Database, group_id: int) -> dict[int, RateLimitRuleSet]:
    # 一次取回全组成员的规则，避免逐个成员查询。
    rows = db.query_all(
        """
        SELECT r.user_id, r.period, r.max_requests, r.is_active
        FROM rate_limit_rules r
        JOIN users u ON u.id = r.user_id
        WHERE u.group_id = ? AND u.deleted_at IS NULL
        ORDER BY r.user_id, r.id
        """,
        (group_id,),
    )
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(int(row["user_id"]), []).append(row)
    return {user_id: RateLimitRuleSet.from_rows(items) for user_id, items in grouped.items()}


def _user_member_values(row: sqlite3.Row, rules: RateLimitRuleSet) -> dict[str, object]:
    source = _with_member_rate_limit_rules(row, rules)
    return {spec.name: spec.user_value(source) for spec in MEMBER_FIELD_SPECS}


def _apply_member_field_updates(
    db: Database,
    quota_manager: QuotaManager,
    members: list[tuple[int, dict[str, object]]],
    fields: Iterable[str],
    values: Mapping[str, object],
    *,
    should_update: Callable[[str, dict[str, object]], bool] | None = None,
    skip_unchanged: bool,
) -> tuple[set[int], dict[str, int]]:
    specs = [_member_field_spec(field) for field in fields]
    field_counts = {spec.name: 0 for spec in specs}
    updated_users: set[int] = set()
    user_updates: dict[int, dict[str, object]] = {}
    quota_updates: dict[int, tuple[int, str, int]] = {}
    rule_updates: dict[int, RateLimitRuleSet] = {}

    for user_id, member_values in members:
        for spec in specs:
            if should_update is not None and not should_update(spec.name, member_values):
                continue
            new_value = values[spec.name]
            if skip_unchanged and member_values[spec.name] == new_value:
                # 已与新组配置一致的成员无需修改，也不计入覆盖人数。
                continue
            if spec.writer == WRITER_QUOTA:
                quota_updates[user_id] = _quota_value(new_value)
            elif spec.writer == WRITER_RATE_LIMIT_RULES:
                rule_updates[user_id] = new_value
            else:
                user_updates.setdefault(user_id, {}).update(spec.user_columns(new_value))
            field_counts[spec.name] += 1
            updated_users.add(user_id)

    if user_updates or rule_updates:
        now = utc_now_iso()
        with db.transaction() as conn:
            for user_id, columns in user_updates.items():
                assignments = ", ".join(f"{column} = ?" for column in columns)
                conn.execute(
                    f"UPDATE users SET {assignments} WHERE id = ? AND deleted_at IS NULL",
                    (*columns.values(), user_id),
                )
            for user_id, rule_set in rule_updates.items():
                replace_user_rate_limit_rules_with_connection(conn, user_id, rule_set, now=now)
    for user_id, (total, period, day) in quota_updates.items():
        quota_manager.create_or_update(user_id, total, period, day)

    return updated_users, field_counts


def _display_member_value(field: str, value: object) -> str:
    return _member_field_spec(field).display_value(value)


def _ordered_member_fields(fields: set[str]) -> list[str]:
    return [spec.name for spec in MEMBER_FIELD_SPECS if spec.name in fields]


def _member_field_spec(field_name: str) -> MemberFieldSpec:
    try:
        return _MEMBER_FIELD_SPECS_BY_NAME[field_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported member field: {field_name}") from exc


def _quota_value(value: object) -> tuple[int, str, int]:
    total, period, day = value
    return int(total), str(period), int(day)
