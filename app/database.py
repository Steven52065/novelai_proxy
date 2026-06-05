from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")

    def init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_key_hash TEXT NOT NULL UNIQUE,
                    api_key TEXT,
                    name TEXT NOT NULL,
                    tier TEXT NOT NULL DEFAULT 'normal',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    free_small_only INTEGER NOT NULL DEFAULT 0,
                    allowed_endpoints TEXT NOT NULL DEFAULT 'generate-image',
                    allowed_upstreams TEXT,
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
                    output_files TEXT,
                    image_urls TEXT,
                    is_retry_success INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(request_id, attempt_number)
                );

                CREATE INDEX IF NOT EXISTS idx_usage_user_created
                    ON usage_logs(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_usage_status
                    ON usage_logs(status);
                CREATE INDEX IF NOT EXISTS idx_usage_request_id
                    ON usage_logs(request_id);

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
            )
            self._add_column_if_missing("usage_logs", "model", "TEXT")
            self._add_column_if_missing("usage_logs", "width", "INTEGER")
            self._add_column_if_missing("usage_logs", "height", "INTEGER")
            self._add_column_if_missing("usage_logs", "steps", "INTEGER")
            self._add_column_if_missing("usage_logs", "n_samples", "INTEGER")
            self._add_column_if_missing("usage_logs", "final_anlas_cost", "INTEGER")
            self._add_column_if_missing("usage_logs", "queued_ms", "INTEGER")
            self._add_column_if_missing("usage_logs", "upstream_ms", "INTEGER")
            self._add_column_if_missing("usage_logs", "total_ms", "INTEGER")
            self._add_column_if_missing("usage_logs", "error_code", "TEXT")
            self._add_column_if_missing("usage_logs", "error_message", "TEXT")
            self._add_column_if_missing("usage_logs", "log_level", "TEXT NOT NULL DEFAULT 'INFO'")
            self._add_column_if_missing("usage_logs", "upstream_id", "TEXT")
            self._add_column_if_missing("usage_logs", "request_payload", "TEXT")
            self._add_column_if_missing("usage_logs", "output_files", "TEXT")
            self._add_column_if_missing("usage_logs", "image_urls", "TEXT")
            self._add_column_if_missing("usage_logs", "is_retry_success", "INTEGER NOT NULL DEFAULT 0")
            self._add_column_if_missing("usage_logs", "attempt_number", "INTEGER NOT NULL DEFAULT 0")
            self._add_column_if_missing("usage_logs", "completed_at", "TEXT")
            self._migrate_usage_logs_unique_constraint()
            self._add_column_if_missing("users", "api_key", "TEXT")
            self._add_column_if_missing("users", "free_small_only", "INTEGER NOT NULL DEFAULT 0")
            self._add_column_if_missing("users", "allowed_endpoints", "TEXT NOT NULL DEFAULT 'generate-image'")
            self._add_column_if_missing("users", "allowed_upstreams", "TEXT")
            self._add_column_if_missing("users", "deleted_at", "TEXT")
            self._clear_stored_user_api_keys()
            self._init_dashboard_hourly_triggers()
            self._backfill_dashboard_hourly_stats_if_empty()

    def rebuild_dashboard_hourly_stats(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                DELETE FROM dashboard_hourly_stats;
                DELETE FROM dashboard_hourly_request_refs;

                INSERT INTO dashboard_hourly_request_refs (bucket_hour, upstream_id, request_id, ref_count)
                SELECT strftime('%Y-%m-%dT%H:00:00+08:00', datetime(created_at, '+8 hours')),
                       '__all__',
                       request_id,
                       COUNT(*)
                FROM usage_logs
                GROUP BY strftime('%Y-%m-%dT%H:00:00+08:00', datetime(created_at, '+8 hours')), request_id;

                INSERT INTO dashboard_hourly_request_refs (bucket_hour, upstream_id, request_id, ref_count)
                SELECT strftime('%Y-%m-%dT%H:00:00+08:00', datetime(created_at, '+8 hours')),
                       upstream_id,
                       request_id,
                       COUNT(*)
                FROM usage_logs
                WHERE upstream_id IS NOT NULL AND upstream_id != ''
                GROUP BY strftime('%Y-%m-%dT%H:00:00+08:00', datetime(created_at, '+8 hours')), upstream_id, request_id;

                INSERT INTO dashboard_hourly_stats (
                    bucket_hour, upstream_id, request_count, success_count, failed_count,
                    rejected_count, retry_success_count, anlas_cost, updated_at
                )
                SELECT strftime('%Y-%m-%dT%H:00:00+08:00', datetime(created_at, '+8 hours')),
                       '__all__',
                       COUNT(DISTINCT request_id),
                       SUM(CASE WHEN lower(status) = 'success' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN lower(status) = 'failed' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN lower(status) = 'rejected' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN lower(status) = 'success' AND is_retry_success = 1 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN lower(status) = 'success' THEN COALESCE(final_anlas_cost, 0) ELSE 0 END),
                       strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                FROM usage_logs
                GROUP BY strftime('%Y-%m-%dT%H:00:00+08:00', datetime(created_at, '+8 hours'));

                INSERT INTO dashboard_hourly_stats (
                    bucket_hour, upstream_id, request_count, success_count, failed_count,
                    rejected_count, retry_success_count, anlas_cost, updated_at
                )
                SELECT strftime('%Y-%m-%dT%H:00:00+08:00', datetime(created_at, '+8 hours')),
                       upstream_id,
                       COUNT(DISTINCT request_id),
                       SUM(CASE WHEN lower(status) = 'success' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN lower(status) = 'failed' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN lower(status) = 'rejected' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN lower(status) = 'success' AND is_retry_success = 1 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN lower(status) = 'success' THEN COALESCE(final_anlas_cost, 0) ELSE 0 END),
                       strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                FROM usage_logs
                WHERE upstream_id IS NOT NULL AND upstream_id != ''
                GROUP BY strftime('%Y-%m-%dT%H:00:00+08:00', datetime(created_at, '+8 hours')), upstream_id;
                """
            )

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.conn
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _clear_stored_user_api_keys(self) -> None:
        # API keys are shown only once on create/reset. Authentication uses api_key_hash.
        self.conn.execute("UPDATE users SET api_key = NULL WHERE api_key IS NOT NULL")

    def _backfill_dashboard_hourly_stats_if_empty(self) -> None:
        has_stats = self.conn.execute("SELECT 1 FROM dashboard_hourly_stats LIMIT 1").fetchone()
        has_logs = self.conn.execute("SELECT 1 FROM usage_logs LIMIT 1").fetchone()
        if has_stats is None and has_logs is not None:
            self.rebuild_dashboard_hourly_stats()

    def _init_dashboard_hourly_triggers(self) -> None:
        self.conn.executescript(
            """
            DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_insert;
            DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_update_old;
            DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_update_new;
            DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_update;
            DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_delete;

            CREATE TRIGGER trg_usage_logs_dashboard_insert
            AFTER INSERT ON usage_logs
            BEGIN
                INSERT INTO dashboard_hourly_request_refs (bucket_hour, upstream_id, request_id, ref_count)
                VALUES (strftime('%Y-%m-%dT%H:00:00+08:00', datetime(NEW.created_at, '+8 hours')), '__all__', NEW.request_id, 1)
                ON CONFLICT(bucket_hour, upstream_id, request_id)
                DO UPDATE SET ref_count = ref_count + 1;

                INSERT INTO dashboard_hourly_stats (
                    bucket_hour, upstream_id, request_count, success_count, failed_count,
                    rejected_count, retry_success_count, anlas_cost, updated_at
                )
                VALUES (
                    strftime('%Y-%m-%dT%H:00:00+08:00', datetime(NEW.created_at, '+8 hours')),
                    '__all__',
                    CASE WHEN (
                        SELECT ref_count
                        FROM dashboard_hourly_request_refs
                        WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(NEW.created_at, '+8 hours'))
                          AND upstream_id = '__all__'
                          AND request_id = NEW.request_id
                    ) = 1 THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'success' THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'failed' THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'rejected' THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'success' AND NEW.is_retry_success = 1 THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'success' THEN COALESCE(NEW.final_anlas_cost, 0) ELSE 0 END,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
                ON CONFLICT(bucket_hour, upstream_id)
                DO UPDATE SET
                    request_count = request_count + excluded.request_count,
                    success_count = success_count + excluded.success_count,
                    failed_count = failed_count + excluded.failed_count,
                    rejected_count = rejected_count + excluded.rejected_count,
                    retry_success_count = retry_success_count + excluded.retry_success_count,
                    anlas_cost = anlas_cost + excluded.anlas_cost,
                    updated_at = excluded.updated_at;

                INSERT INTO dashboard_hourly_request_refs (bucket_hour, upstream_id, request_id, ref_count)
                SELECT strftime('%Y-%m-%dT%H:00:00+08:00', datetime(NEW.created_at, '+8 hours')), NEW.upstream_id, NEW.request_id, 1
                WHERE NEW.upstream_id IS NOT NULL AND NEW.upstream_id != ''
                ON CONFLICT(bucket_hour, upstream_id, request_id)
                DO UPDATE SET ref_count = ref_count + 1;

                INSERT INTO dashboard_hourly_stats (
                    bucket_hour, upstream_id, request_count, success_count, failed_count,
                    rejected_count, retry_success_count, anlas_cost, updated_at
                )
                SELECT
                    strftime('%Y-%m-%dT%H:00:00+08:00', datetime(NEW.created_at, '+8 hours')),
                    NEW.upstream_id,
                    CASE WHEN (
                        SELECT ref_count
                        FROM dashboard_hourly_request_refs
                        WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(NEW.created_at, '+8 hours'))
                          AND upstream_id = NEW.upstream_id
                          AND request_id = NEW.request_id
                    ) = 1 THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'success' THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'failed' THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'rejected' THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'success' AND NEW.is_retry_success = 1 THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'success' THEN COALESCE(NEW.final_anlas_cost, 0) ELSE 0 END,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE NEW.upstream_id IS NOT NULL AND NEW.upstream_id != ''
                ON CONFLICT(bucket_hour, upstream_id)
                DO UPDATE SET
                    request_count = request_count + excluded.request_count,
                    success_count = success_count + excluded.success_count,
                    failed_count = failed_count + excluded.failed_count,
                    rejected_count = rejected_count + excluded.rejected_count,
                    retry_success_count = retry_success_count + excluded.retry_success_count,
                    anlas_cost = anlas_cost + excluded.anlas_cost,
                    updated_at = excluded.updated_at;
            END;

            CREATE TRIGGER trg_usage_logs_dashboard_update
            AFTER UPDATE OF created_at, request_id, upstream_id, status, is_retry_success, final_anlas_cost ON usage_logs
            BEGIN
                UPDATE dashboard_hourly_stats
                SET success_count = success_count - CASE WHEN lower(OLD.status) = 'success' THEN 1 ELSE 0 END,
                    failed_count = failed_count - CASE WHEN lower(OLD.status) = 'failed' THEN 1 ELSE 0 END,
                    rejected_count = rejected_count - CASE WHEN lower(OLD.status) = 'rejected' THEN 1 ELSE 0 END,
                    retry_success_count = retry_success_count - CASE WHEN lower(OLD.status) = 'success' AND OLD.is_retry_success = 1 THEN 1 ELSE 0 END,
                    anlas_cost = anlas_cost - CASE WHEN lower(OLD.status) = 'success' THEN COALESCE(OLD.final_anlas_cost, 0) ELSE 0 END,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = '__all__';

                UPDATE dashboard_hourly_stats
                SET success_count = success_count - CASE WHEN lower(OLD.status) = 'success' THEN 1 ELSE 0 END,
                    failed_count = failed_count - CASE WHEN lower(OLD.status) = 'failed' THEN 1 ELSE 0 END,
                    rejected_count = rejected_count - CASE WHEN lower(OLD.status) = 'rejected' THEN 1 ELSE 0 END,
                    retry_success_count = retry_success_count - CASE WHEN lower(OLD.status) = 'success' AND OLD.is_retry_success = 1 THEN 1 ELSE 0 END,
                    anlas_cost = anlas_cost - CASE WHEN lower(OLD.status) = 'success' THEN COALESCE(OLD.final_anlas_cost, 0) ELSE 0 END,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = OLD.upstream_id
                  AND OLD.upstream_id IS NOT NULL AND OLD.upstream_id != '';

                UPDATE dashboard_hourly_request_refs
                SET ref_count = ref_count - 1
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = '__all__'
                  AND request_id = OLD.request_id;

                UPDATE dashboard_hourly_stats
                SET request_count = request_count - 1,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = '__all__'
                  AND (
                      SELECT ref_count
                      FROM dashboard_hourly_request_refs
                      WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                        AND upstream_id = '__all__'
                        AND request_id = OLD.request_id
                  ) = 0;

                DELETE FROM dashboard_hourly_request_refs
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = '__all__'
                  AND request_id = OLD.request_id
                  AND ref_count <= 0;

                UPDATE dashboard_hourly_request_refs
                SET ref_count = ref_count - 1
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = OLD.upstream_id
                  AND request_id = OLD.request_id
                  AND OLD.upstream_id IS NOT NULL AND OLD.upstream_id != '';

                UPDATE dashboard_hourly_stats
                SET request_count = request_count - 1,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = OLD.upstream_id
                  AND OLD.upstream_id IS NOT NULL AND OLD.upstream_id != ''
                  AND (
                      SELECT ref_count
                      FROM dashboard_hourly_request_refs
                      WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                        AND upstream_id = OLD.upstream_id
                        AND request_id = OLD.request_id
                  ) = 0;

                DELETE FROM dashboard_hourly_request_refs
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = OLD.upstream_id
                  AND request_id = OLD.request_id
                  AND OLD.upstream_id IS NOT NULL AND OLD.upstream_id != ''
                  AND ref_count <= 0;

                INSERT INTO dashboard_hourly_request_refs (bucket_hour, upstream_id, request_id, ref_count)
                VALUES (strftime('%Y-%m-%dT%H:00:00+08:00', datetime(NEW.created_at, '+8 hours')), '__all__', NEW.request_id, 1)
                ON CONFLICT(bucket_hour, upstream_id, request_id)
                DO UPDATE SET ref_count = ref_count + 1;

                INSERT INTO dashboard_hourly_stats (
                    bucket_hour, upstream_id, request_count, success_count, failed_count,
                    rejected_count, retry_success_count, anlas_cost, updated_at
                )
                VALUES (
                    strftime('%Y-%m-%dT%H:00:00+08:00', datetime(NEW.created_at, '+8 hours')),
                    '__all__',
                    CASE WHEN (
                        SELECT ref_count
                        FROM dashboard_hourly_request_refs
                        WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(NEW.created_at, '+8 hours'))
                          AND upstream_id = '__all__'
                          AND request_id = NEW.request_id
                    ) = 1 THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'success' THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'failed' THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'rejected' THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'success' AND NEW.is_retry_success = 1 THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'success' THEN COALESCE(NEW.final_anlas_cost, 0) ELSE 0 END,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
                ON CONFLICT(bucket_hour, upstream_id)
                DO UPDATE SET
                    request_count = request_count + excluded.request_count,
                    success_count = success_count + excluded.success_count,
                    failed_count = failed_count + excluded.failed_count,
                    rejected_count = rejected_count + excluded.rejected_count,
                    retry_success_count = retry_success_count + excluded.retry_success_count,
                    anlas_cost = anlas_cost + excluded.anlas_cost,
                    updated_at = excluded.updated_at;

                INSERT INTO dashboard_hourly_request_refs (bucket_hour, upstream_id, request_id, ref_count)
                SELECT strftime('%Y-%m-%dT%H:00:00+08:00', datetime(NEW.created_at, '+8 hours')), NEW.upstream_id, NEW.request_id, 1
                WHERE NEW.upstream_id IS NOT NULL AND NEW.upstream_id != ''
                ON CONFLICT(bucket_hour, upstream_id, request_id)
                DO UPDATE SET ref_count = ref_count + 1;

                INSERT INTO dashboard_hourly_stats (
                    bucket_hour, upstream_id, request_count, success_count, failed_count,
                    rejected_count, retry_success_count, anlas_cost, updated_at
                )
                SELECT
                    strftime('%Y-%m-%dT%H:00:00+08:00', datetime(NEW.created_at, '+8 hours')),
                    NEW.upstream_id,
                    CASE WHEN (
                        SELECT ref_count
                        FROM dashboard_hourly_request_refs
                        WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(NEW.created_at, '+8 hours'))
                          AND upstream_id = NEW.upstream_id
                          AND request_id = NEW.request_id
                    ) = 1 THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'success' THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'failed' THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'rejected' THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'success' AND NEW.is_retry_success = 1 THEN 1 ELSE 0 END,
                    CASE WHEN lower(NEW.status) = 'success' THEN COALESCE(NEW.final_anlas_cost, 0) ELSE 0 END,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE NEW.upstream_id IS NOT NULL AND NEW.upstream_id != ''
                ON CONFLICT(bucket_hour, upstream_id)
                DO UPDATE SET
                    request_count = request_count + excluded.request_count,
                    success_count = success_count + excluded.success_count,
                    failed_count = failed_count + excluded.failed_count,
                    rejected_count = rejected_count + excluded.rejected_count,
                    retry_success_count = retry_success_count + excluded.retry_success_count,
                    anlas_cost = anlas_cost + excluded.anlas_cost,
                    updated_at = excluded.updated_at;
            END;

            CREATE TRIGGER trg_usage_logs_dashboard_delete
            AFTER DELETE ON usage_logs
            BEGIN
                UPDATE dashboard_hourly_stats
                SET success_count = success_count - CASE WHEN lower(OLD.status) = 'success' THEN 1 ELSE 0 END,
                    failed_count = failed_count - CASE WHEN lower(OLD.status) = 'failed' THEN 1 ELSE 0 END,
                    rejected_count = rejected_count - CASE WHEN lower(OLD.status) = 'rejected' THEN 1 ELSE 0 END,
                    retry_success_count = retry_success_count - CASE WHEN lower(OLD.status) = 'success' AND OLD.is_retry_success = 1 THEN 1 ELSE 0 END,
                    anlas_cost = anlas_cost - CASE WHEN lower(OLD.status) = 'success' THEN COALESCE(OLD.final_anlas_cost, 0) ELSE 0 END,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = '__all__';

                UPDATE dashboard_hourly_request_refs
                SET ref_count = ref_count - 1
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = '__all__'
                  AND request_id = OLD.request_id;

                UPDATE dashboard_hourly_stats
                SET request_count = request_count - 1,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = '__all__'
                  AND (
                      SELECT ref_count
                      FROM dashboard_hourly_request_refs
                      WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                        AND upstream_id = '__all__'
                        AND request_id = OLD.request_id
                  ) = 0;

                DELETE FROM dashboard_hourly_request_refs
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = '__all__'
                  AND request_id = OLD.request_id
                  AND ref_count <= 0;

                UPDATE dashboard_hourly_stats
                SET success_count = success_count - CASE WHEN lower(OLD.status) = 'success' THEN 1 ELSE 0 END,
                    failed_count = failed_count - CASE WHEN lower(OLD.status) = 'failed' THEN 1 ELSE 0 END,
                    rejected_count = rejected_count - CASE WHEN lower(OLD.status) = 'rejected' THEN 1 ELSE 0 END,
                    retry_success_count = retry_success_count - CASE WHEN lower(OLD.status) = 'success' AND OLD.is_retry_success = 1 THEN 1 ELSE 0 END,
                    anlas_cost = anlas_cost - CASE WHEN lower(OLD.status) = 'success' THEN COALESCE(OLD.final_anlas_cost, 0) ELSE 0 END,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = OLD.upstream_id
                  AND OLD.upstream_id IS NOT NULL AND OLD.upstream_id != '';

                UPDATE dashboard_hourly_request_refs
                SET ref_count = ref_count - 1
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = OLD.upstream_id
                  AND request_id = OLD.request_id
                  AND OLD.upstream_id IS NOT NULL AND OLD.upstream_id != '';

                UPDATE dashboard_hourly_stats
                SET request_count = request_count - 1,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = OLD.upstream_id
                  AND OLD.upstream_id IS NOT NULL AND OLD.upstream_id != ''
                  AND (
                      SELECT ref_count
                      FROM dashboard_hourly_request_refs
                      WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                        AND upstream_id = OLD.upstream_id
                        AND request_id = OLD.request_id
                  ) = 0;

                DELETE FROM dashboard_hourly_request_refs
                WHERE bucket_hour = strftime('%Y-%m-%dT%H:00:00+08:00', datetime(OLD.created_at, '+8 hours'))
                  AND upstream_id = OLD.upstream_id
                  AND request_id = OLD.request_id
                  AND OLD.upstream_id IS NOT NULL AND OLD.upstream_id != ''
                  AND ref_count <= 0;
            END;
            """
        )

    def _migrate_usage_logs_unique_constraint(self) -> None:
        """迁移 usage_logs 表的唯一约束，从 request_id 改为 (request_id, attempt_number)"""
        table_info = self.conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='usage_logs'").fetchone()
        if table_info is None:
            return
        table_sql = table_info["sql"] or ""
        normalized_sql = " ".join(table_sql.replace("\n", " ").split()).lower()
        if "unique(request_id, attempt_number)" in normalized_sql:
            return
        if "request_id text not null unique" not in normalized_sql:
            return

        # 执行迁移：重建表
        self.conn.executescript("""
            -- 创建新表
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
                output_files TEXT,
                image_urls TEXT,
                is_retry_success INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(request_id, attempt_number)
            );

            -- 复制旧数据（所有记录的 attempt_number 默认为 0）
            INSERT INTO usage_logs_new (
                id, request_id, attempt_number, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, final_anlas_cost, queued_ms, upstream_ms, total_ms, status, error_code, error_message,
                log_level, upstream_id, request_payload, output_files, image_urls, is_retry_success,
                created_at, completed_at
            )
            SELECT
                id, request_id, 0, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, final_anlas_cost, queued_ms, upstream_ms, total_ms, status, error_code, error_message,
                log_level, upstream_id, request_payload, output_files, image_urls, is_retry_success,
                created_at, completed_at
            FROM usage_logs;

            -- 删除旧表
            DROP TABLE usage_logs;

            -- 重命名新表
            ALTER TABLE usage_logs_new RENAME TO usage_logs;

            -- 重建索引
            CREATE INDEX IF NOT EXISTS idx_usage_user_created
                ON usage_logs(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_usage_status
                ON usage_logs(status);
            CREATE INDEX IF NOT EXISTS idx_usage_request_id
                ON usage_logs(request_id);
        """)
