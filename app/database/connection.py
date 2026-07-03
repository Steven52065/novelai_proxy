from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .dashboard_cache import (
    backfill_dashboard_hourly_stats_if_empty,
    init_dashboard_hourly_triggers,
    rebuild_dashboard_hourly_stats,
)
from .migrations import (
    add_column_if_missing,
    clear_stored_user_api_keys,
    migrate_group_daily_limit_into_members,
    migrate_usage_log_size_fields,
    migrate_usage_logs_unique_constraint,
)
from .schema import initialize_schema


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
        initialize_schema(self)

    def rebuild_dashboard_hourly_stats(self) -> None:
        with self._lock:
            rebuild_dashboard_hourly_stats(self.conn)

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

    def vacuum(self) -> dict[str, int | bool]:
        before = self._total_file_size()
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA busy_timeout = 60000")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        after = self._total_file_size()
        return {
            "ok": True,
            "before_bytes": before,
            "after_bytes": after,
            "reclaimed_bytes": max(before - after, 0),
        }

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _total_file_size(self) -> int:
        return sum(
            file_path.stat().st_size if file_path.exists() else 0
            for file_path in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm"))
        )

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        add_column_if_missing(self.conn, table, column, definition)

    def _clear_stored_user_api_keys(self) -> None:
        clear_stored_user_api_keys(self.conn)

    def _backfill_dashboard_hourly_stats_if_empty(self) -> None:
        backfill_dashboard_hourly_stats_if_empty(self.conn)

    def _init_dashboard_hourly_triggers(self) -> None:
        init_dashboard_hourly_triggers(self.conn)

    def _migrate_group_daily_limit_into_members(self) -> None:
        migrate_group_daily_limit_into_members(self.conn)

    def _migrate_usage_log_size_fields(self) -> None:
        migrate_usage_log_size_fields(self.conn)

    def _migrate_usage_logs_unique_constraint(self) -> None:
        migrate_usage_logs_unique_constraint(self.conn)
