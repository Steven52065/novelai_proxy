from __future__ import annotations

import sqlite3

from ..dashboard_stats import dashboard_triggers_script, rebuild_dashboard_hourly_stats_script


def rebuild_dashboard_hourly_stats(conn: sqlite3.Connection) -> None:
    conn.executescript(rebuild_dashboard_hourly_stats_script())


def init_dashboard_hourly_triggers(conn: sqlite3.Connection) -> None:
    conn.executescript(dashboard_triggers_script())


def backfill_dashboard_hourly_stats_if_empty(conn: sqlite3.Connection) -> None:
    has_stats = conn.execute("SELECT 1 FROM dashboard_hourly_stats LIMIT 1").fetchone()
    has_logs = conn.execute("SELECT 1 FROM usage_logs LIMIT 1").fetchone()
    if has_stats is None and has_logs is not None:
        rebuild_dashboard_hourly_stats(conn)
