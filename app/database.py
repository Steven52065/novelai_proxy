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
                    request_id TEXT NOT NULL UNIQUE,
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
                    status TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    log_level TEXT NOT NULL DEFAULT 'INFO',
                    request_payload TEXT,
                    output_files TEXT,
                    image_urls TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_usage_user_created
                    ON usage_logs(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_usage_status
                    ON usage_logs(status);
                """
            )
            self._add_column_if_missing("usage_logs", "log_level", "TEXT NOT NULL DEFAULT 'INFO'")
            self._add_column_if_missing("usage_logs", "request_payload", "TEXT")
            self._add_column_if_missing("usage_logs", "output_files", "TEXT")
            self._add_column_if_missing("usage_logs", "image_urls", "TEXT")
            self._add_column_if_missing("users", "api_key", "TEXT")
            self._add_column_if_missing("users", "free_small_only", "INTEGER NOT NULL DEFAULT 0")
            self._add_column_if_missing("users", "allowed_endpoints", "TEXT NOT NULL DEFAULT 'generate-image'")

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
