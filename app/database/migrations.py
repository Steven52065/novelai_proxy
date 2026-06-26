from __future__ import annotations

import sqlite3


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def clear_stored_user_api_keys(conn: sqlite3.Connection) -> None:
    # API keys are shown only once on create/reset. Authentication uses api_key_hash.
    conn.execute("UPDATE users SET api_key = NULL WHERE api_key IS NOT NULL")


def migrate_group_daily_limit_into_members(conn: sqlite3.Connection) -> None:
    """一次性迁移：把用户组的每日免费小图限制复制到成员的用户级配置。

    每日限制曾经在运行时按"用户与组中较严格者"生效；改为仅以用户自身配置生效后，
    需要把启用了该限制的启用组的配置，复制给组内未单独启用限制的用户，
    避免升级后这些用户的限制凭空消失。
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 1:
        return
    conn.execute(
        """
        UPDATE users
        SET free_small_daily_limit_enabled = (
                SELECT g.free_small_daily_limit_enabled FROM user_groups g WHERE g.id = users.group_id
            ),
            free_small_daily_limit = (
                SELECT g.free_small_daily_limit FROM user_groups g WHERE g.id = users.group_id
            )
        WHERE deleted_at IS NULL
          AND free_small_daily_limit_enabled = 0
          AND group_id IN (
              SELECT id FROM user_groups WHERE is_active = 1 AND free_small_daily_limit_enabled = 1
          )
        """
    )
    conn.execute("PRAGMA user_version = 1")


def migrate_usage_logs_unique_constraint(conn: sqlite3.Connection) -> None:
    """迁移 usage_logs 表的唯一约束，从 request_id 改为 (request_id, attempt_number)。"""
    table_info = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='usage_logs'").fetchone()
    if table_info is None:
        return
    table_sql = table_info["sql"] or ""
    normalized_sql = " ".join(table_sql.replace("\n", " ").split()).lower()
    if "unique(request_id, attempt_number)" in normalized_sql:
        return
    if "request_id text not null unique" not in normalized_sql:
        return

    conn.executescript(
        """
        CREATE TABLE usage_logs_new (
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
            request_payload_available_bytes INTEGER NOT NULL DEFAULT 0,
            request_payload_compressed_bytes INTEGER NOT NULL DEFAULT 0,
            output_files TEXT,
            output_files_bytes INTEGER NOT NULL DEFAULT 0,
            image_urls TEXT,
            image_urls_bytes INTEGER NOT NULL DEFAULT 0,
            is_retry_success INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(request_id, attempt_number)
        );

        INSERT INTO usage_logs_new (
            id, request_id, attempt_number, user_id, action, model, width, height, steps, n_samples,
            estimated_anlas_cost, final_anlas_cost, queued_ms, upstream_ms, total_ms, status, error_code, error_message,
            log_level, upstream_id, request_payload, request_payload_encoding, request_payload_blob,
            request_payload_bytes, request_payload_available_bytes, request_payload_compressed_bytes,
            output_files, output_files_bytes, image_urls, image_urls_bytes, is_retry_success,
            created_at, completed_at
        )
        SELECT
            id, request_id, 0, user_id, action, model, width, height, steps, n_samples,
            estimated_anlas_cost, final_anlas_cost, queued_ms, upstream_ms, total_ms, status, error_code, error_message,
            log_level, upstream_id, request_payload,
            COALESCE(NULLIF(request_payload_encoding, ''), 'json'),
            request_payload_blob,
            CASE
                WHEN request_payload_bytes > 0 THEN request_payload_bytes
                WHEN request_payload IS NOT NULL AND request_payload != '' THEN LENGTH(CAST(request_payload AS BLOB))
                ELSE 0
            END,
            CASE
                WHEN request_payload_bytes > 0 THEN request_payload_bytes
                WHEN request_payload IS NOT NULL AND request_payload != '' THEN LENGTH(CAST(request_payload AS BLOB))
                ELSE 0
            END,
            COALESCE(request_payload_compressed_bytes, 0),
            output_files,
            COALESCE(LENGTH(CAST(output_files AS BLOB)), 0),
            image_urls,
            COALESCE(LENGTH(CAST(image_urls AS BLOB)), 0),
            is_retry_success,
            created_at, completed_at
        FROM usage_logs;

        DROP TABLE usage_logs;

        ALTER TABLE usage_logs_new RENAME TO usage_logs;

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
        CREATE INDEX IF NOT EXISTS idx_usage_size_report
            ON usage_logs(request_payload_available_bytes DESC, output_files_bytes DESC, image_urls_bytes DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_usage_hot_payload_created
            ON usage_logs(created_at, id)
            WHERE request_payload_bytes > 0;
        """
    )


def migrate_usage_log_size_fields(conn: sqlite3.Connection) -> None:
    """Backfill usage_logs precomputed byte counters introduced in user_version 2."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 2:
        return

    conn.execute(
        """
        UPDATE usage_logs
        SET request_payload_bytes = LENGTH(CAST(request_payload AS BLOB))
        WHERE COALESCE(request_payload_bytes, 0) = 0
          AND request_payload IS NOT NULL
          AND request_payload != ''
        """
    )
    conn.execute(
        """
        UPDATE usage_logs
        SET request_payload_available_bytes = COALESCE(
                (
                    SELECT r.payload_bytes
                    FROM usage_log_payload_archive_refs r
                    WHERE r.log_id = usage_logs.id
                ),
                CASE
                    WHEN request_payload_bytes > 0 THEN request_payload_bytes
                    WHEN request_payload IS NOT NULL AND request_payload != '' THEN LENGTH(CAST(request_payload AS BLOB))
                    ELSE 0
                END
            ),
            output_files_bytes = COALESCE(LENGTH(CAST(output_files AS BLOB)), 0),
            image_urls_bytes = COALESCE(LENGTH(CAST(image_urls AS BLOB)), 0)
        """
    )
    conn.execute("PRAGMA user_version = 2")
