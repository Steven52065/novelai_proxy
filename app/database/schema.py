from __future__ import annotations

from typing import Any

BASE_SCHEMA_SQL = """
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

CREATE TABLE IF NOT EXISTS usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    model TEXT,
    width INTEGER,
    height INTEGER,
    steps INTEGER,
    n_samples INTEGER,
    estimated_anlas_cost INTEGER NOT NULL DEFAULT 0,
    final_anlas_cost INTEGER,
    queued_ms INTEGER,
    upstream_ms INTEGER,
    total_ms INTEGER,
    status TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    log_level TEXT NOT NULL DEFAULT 'INFO',
    upstream_id TEXT,
    request_payload TEXT,
    request_payload_encoding TEXT NOT NULL DEFAULT 'json',
    request_payload_blob BLOB,
    request_payload_bytes INTEGER NOT NULL DEFAULT 0,
    request_payload_compressed_bytes INTEGER NOT NULL DEFAULT 0,
    output_files TEXT,
    image_urls TEXT,
    is_retry_success INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(request_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS idx_usage_user_created
    ON usage_logs(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_created_at
    ON usage_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_action_created
    ON usage_logs(action, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_status_created
    ON usage_logs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_status
    ON usage_logs(status);
CREATE INDEX IF NOT EXISTS idx_usage_request_id
    ON usage_logs(request_id);

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
"""

USAGE_LOGS_COLUMNS = (
    ("model", "TEXT"),
    ("width", "INTEGER"),
    ("height", "INTEGER"),
    ("steps", "INTEGER"),
    ("n_samples", "INTEGER"),
    ("final_anlas_cost", "INTEGER"),
    ("queued_ms", "INTEGER"),
    ("upstream_ms", "INTEGER"),
    ("total_ms", "INTEGER"),
    ("error_code", "TEXT"),
    ("error_message", "TEXT"),
    ("log_level", "TEXT NOT NULL DEFAULT 'INFO'"),
    ("upstream_id", "TEXT"),
    ("request_payload", "TEXT"),
    ("request_payload_encoding", "TEXT NOT NULL DEFAULT 'json'"),
    ("request_payload_blob", "BLOB"),
    ("request_payload_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("request_payload_compressed_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("output_files", "TEXT"),
    ("image_urls", "TEXT"),
    ("is_retry_success", "INTEGER NOT NULL DEFAULT 0"),
    ("attempt_number", "INTEGER NOT NULL DEFAULT 0"),
    ("completed_at", "TEXT"),
)

USERS_COLUMNS = (
    ("api_key", "TEXT"),
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
        db._migrate_group_daily_limit_into_members()
        db.conn.execute("CREATE INDEX IF NOT EXISTS idx_users_group_id ON users(group_id)")
        db.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_group_rate_limit_rules_group_id ON group_rate_limit_rules(group_id)"
        )
        db.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_payload_archives_date "
            "ON usage_log_payload_archives(archive_date, part_number)"
        )
        db.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_payload_archive_refs_archive "
            "ON usage_log_payload_archive_refs(archive_id)"
        )
        db._clear_stored_user_api_keys()
        db._init_dashboard_hourly_triggers()
        db._backfill_dashboard_hourly_stats_if_empty()
