from __future__ import annotations

from typing import Any

USAGE_LOGS_COLUMN_DEFS = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("request_id", "TEXT NOT NULL"),
    ("attempt_number", "INTEGER NOT NULL DEFAULT 0"),
    ("user_id", "INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE"),
    ("action", "TEXT NOT NULL"),
    ("model", "TEXT"),
    ("width", "INTEGER"),
    ("height", "INTEGER"),
    ("steps", "INTEGER"),
    ("n_samples", "INTEGER"),
    ("estimated_anlas_cost", "INTEGER NOT NULL DEFAULT 0"),
    ("final_anlas_cost", "INTEGER"),
    ("queued_ms", "INTEGER"),
    ("upstream_ms", "INTEGER"),
    ("total_ms", "INTEGER"),
    ("status", "TEXT NOT NULL"),
    ("error_code", "TEXT"),
    ("error_message", "TEXT"),
    ("log_level", "TEXT NOT NULL DEFAULT 'INFO'"),
    ("upstream_id", "TEXT"),
    ("request_payload", "TEXT"),
    ("request_payload_encoding", "TEXT NOT NULL DEFAULT 'json'"),
    ("request_payload_blob", "BLOB"),
    ("request_payload_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("request_payload_available_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("request_payload_compressed_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("output_files", "TEXT"),
    ("output_files_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("image_urls", "TEXT"),
    ("image_urls_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("is_retry_success", "INTEGER NOT NULL DEFAULT 0"),
    ("created_at", "TEXT NOT NULL"),
    ("completed_at", "TEXT"),
)

_USAGE_LOGS_INITIAL_COLUMNS = {
    "id",
    "request_id",
    "user_id",
    "action",
    "estimated_anlas_cost",
    "status",
    "created_at",
}
USAGE_LOGS_COLUMNS = tuple(
    (column, definition)
    for column, definition in USAGE_LOGS_COLUMN_DEFS
    if column not in _USAGE_LOGS_INITIAL_COLUMNS
)

USAGE_LOGS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_usage_user_created ON usage_logs(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_usage_created_at ON usage_logs(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_usage_action_created ON usage_logs(action, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_usage_status_created ON usage_logs(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_usage_status ON usage_logs(status)",
    "CREATE INDEX IF NOT EXISTS idx_usage_request_id ON usage_logs(request_id)",
    (
        "CREATE INDEX IF NOT EXISTS idx_usage_size_report "
        "ON usage_logs(request_payload_available_bytes DESC, output_files_bytes DESC, image_urls_bytes DESC, id DESC)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_usage_hot_payload_created "
        "ON usage_logs(created_at, id) WHERE request_payload_bytes > 0"
    ),
)


def usage_logs_create_table_sql(table_name: str, *, if_not_exists: bool) -> str:
    exists_clause = "IF NOT EXISTS " if if_not_exists else ""
    columns = ",\n".join(f"    {column} {definition}" for column, definition in USAGE_LOGS_COLUMN_DEFS)
    return f"""CREATE TABLE {exists_clause}{table_name} (
{columns},
    UNIQUE(request_id, attempt_number)
)"""


BASE_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS user_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    default_tier TEXT NOT NULL DEFAULT 'normal',
    default_free_small_only INTEGER NOT NULL DEFAULT 1,
    free_small_daily_limit_enabled INTEGER NOT NULL DEFAULT 0,
    free_small_daily_limit INTEGER NOT NULL DEFAULT 0,
    default_allowed_endpoints TEXT NOT NULL DEFAULT 'generate-image',
    default_allowed_upstreams TEXT,
    default_image_format_policy TEXT NOT NULL DEFAULT 'follow_global',
    default_anlas_total INTEGER NOT NULL DEFAULT 0,
    default_reset_period TEXT NOT NULL DEFAULT 'month',
    default_reset_day INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_hash TEXT NOT NULL UNIQUE,
    api_key TEXT,
    name TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'normal',
    is_active INTEGER NOT NULL DEFAULT 1,
    disabled_by_discord_verification INTEGER NOT NULL DEFAULT 0,
    free_small_only INTEGER NOT NULL DEFAULT 0,
    free_small_daily_limit_enabled INTEGER NOT NULL DEFAULT 0,
    free_small_daily_limit INTEGER NOT NULL DEFAULT 0,
    allowed_endpoints TEXT NOT NULL DEFAULT 'generate-image',
    allowed_upstreams TEXT,
    image_format_policy TEXT NOT NULL DEFAULT 'follow_global',
    group_id INTEGER REFERENCES user_groups(id) ON DELETE SET NULL,
    deleted_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limit_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    max_requests INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_rate_limit_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    max_requests INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- 用户独享限流模板：保存组配置时展开写入成员各自的 rate_limit_rules，
-- 与 group_rate_limit_rules（全组共享一个计数池）语义不同，两表并存互不影响。
CREATE TABLE IF NOT EXISTS group_member_rate_limit_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    max_requests INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discord_user_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    discord_user_id TEXT NOT NULL UNIQUE,
    discord_username TEXT,
    discord_global_name TEXT,
    discord_avatar TEXT,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS free_small_daily_usage (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    window_start TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    reserved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, window_start)
);

CREATE TABLE IF NOT EXISTS user_anlas_quota (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    total INTEGER NOT NULL DEFAULT 0,
    used INTEGER NOT NULL DEFAULT 0,
    reserved INTEGER NOT NULL DEFAULT 0,
    reset_period TEXT NOT NULL DEFAULT 'month',
    reset_day INTEGER NOT NULL DEFAULT 1,
    last_reset_at TEXT,
    created_at TEXT NOT NULL
);

{usage_logs_create_table_sql("usage_logs", if_not_exists=True)};

CREATE TABLE IF NOT EXISTS usage_log_payload_archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_date TEXT NOT NULL,
    part_number INTEGER NOT NULL,
    compression TEXT NOT NULL,
    compression_level INTEGER NOT NULL,
    payload_count INTEGER NOT NULL,
    raw_bytes INTEGER NOT NULL,
    compressed_bytes INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_blob BLOB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(archive_date, part_number)
);

CREATE TABLE IF NOT EXISTS usage_log_payload_archive_refs (
    log_id INTEGER PRIMARY KEY REFERENCES usage_logs(id) ON DELETE CASCADE,
    archive_id INTEGER NOT NULL REFERENCES usage_log_payload_archives(id) ON DELETE CASCADE,
    payload_key TEXT NOT NULL,
    payload_bytes INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_payload_archives_date
    ON usage_log_payload_archives(archive_date, part_number);
CREATE INDEX IF NOT EXISTS idx_usage_payload_archive_refs_archive
    ON usage_log_payload_archive_refs(archive_id);

CREATE TABLE IF NOT EXISTS dashboard_hourly_stats (
    bucket_hour TEXT NOT NULL,
    upstream_id TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    retry_success_count INTEGER NOT NULL DEFAULT 0,
    anlas_cost INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(bucket_hour, upstream_id)
);

CREATE INDEX IF NOT EXISTS idx_dashboard_hourly_upstream_bucket
    ON dashboard_hourly_stats(upstream_id, bucket_hour);

CREATE TABLE IF NOT EXISTS dashboard_hourly_request_refs (
    bucket_hour TEXT NOT NULL,
    upstream_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    ref_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(bucket_hour, upstream_id, request_id)
);

CREATE INDEX IF NOT EXISTS idx_dashboard_hourly_refs_upstream_bucket
    ON dashboard_hourly_request_refs(upstream_id, bucket_hour, request_id);

CREATE TABLE IF NOT EXISTS admin_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    event_time TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL,
    dismissed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_admin_notifications_pending
    ON admin_notifications(dismissed_at, event_time, id);

CREATE TABLE IF NOT EXISTS novelai_upstreams (
    id TEXT PRIMARY KEY,
    api_key TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    owner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS novelai_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    account_tier INTEGER NOT NULL DEFAULT 3,
    upscale_anlas_cost INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
"""

USERS_COLUMNS = (
    ("api_key", "TEXT"),
    # 默认 0 表示“停用来源未知”，升级前就已停用的存量账号因此不会被自助登录自动恢复。
    ("disabled_by_discord_verification", "INTEGER NOT NULL DEFAULT 0"),
    ("free_small_only", "INTEGER NOT NULL DEFAULT 0"),
    ("free_small_daily_limit_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("free_small_daily_limit", "INTEGER NOT NULL DEFAULT 0"),
    ("allowed_endpoints", "TEXT NOT NULL DEFAULT 'generate-image'"),
    ("allowed_upstreams", "TEXT"),
    ("image_format_policy", "TEXT NOT NULL DEFAULT 'follow_global'"),
    ("group_id", "INTEGER REFERENCES user_groups(id) ON DELETE SET NULL"),
    ("deleted_at", "TEXT"),
)

USER_GROUPS_COLUMNS = (
    ("free_small_daily_limit_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("free_small_daily_limit", "INTEGER NOT NULL DEFAULT 0"),
    ("default_image_format_policy", "TEXT NOT NULL DEFAULT 'follow_global'"),
)

NOVELAI_UPSTREAMS_COLUMNS = (
    ("owner_user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
)


def initialize_schema(db: Any) -> None:
    with db._lock:
        db.conn.executescript(BASE_SCHEMA_SQL)
        for column, definition in USAGE_LOGS_COLUMNS:
            db._add_column_if_missing("usage_logs", column, definition)
        db._migrate_usage_logs_unique_constraint()
        for column, definition in USERS_COLUMNS:
            db._add_column_if_missing("users", column, definition)
        for column, definition in USER_GROUPS_COLUMNS:
            db._add_column_if_missing("user_groups", column, definition)
        for column, definition in NOVELAI_UPSTREAMS_COLUMNS:
            db._add_column_if_missing("novelai_upstreams", column, definition)
        db._migrate_group_daily_limit_into_members()
        for sql in USAGE_LOGS_INDEX_SQL:
            db.conn.execute(sql)
        db.conn.execute("CREATE INDEX IF NOT EXISTS idx_users_group_id ON users(group_id)")
        db.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_group_rate_limit_rules_group_id ON group_rate_limit_rules(group_id)"
        )
        db.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_group_member_rate_limit_rules_group_id "
            "ON group_member_rate_limit_rules(group_id)"
        )
        db.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rate_limit_rules_user_id ON rate_limit_rules(user_id)"
        )
        db.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_payload_archives_date "
            "ON usage_log_payload_archives(archive_date, part_number)"
        )
        db.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_payload_archive_refs_archive "
            "ON usage_log_payload_archive_refs(archive_id)"
        )
        db.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_notifications_pending "
            "ON admin_notifications(dismissed_at, event_time, id)"
        )
        db.conn.execute(
            """
            INSERT INTO novelai_settings(id, account_tier, upscale_anlas_cost, created_at)
            VALUES (1, 3, 0, datetime('now'))
            ON CONFLICT(id) DO NOTHING
            """
        )
        db._clear_stored_user_api_keys()
        db._migrate_usage_log_size_fields()
        db._init_dashboard_hourly_triggers()
        db._backfill_dashboard_hourly_stats_if_empty()
