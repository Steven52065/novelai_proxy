from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.dashboard_stats import (
    dashboard_triggers_script,
    hour_bucket,
    rebuild_dashboard_hourly_stats_script,
    sql_hour_bucket,
)


def test_hour_bucket_matches_sql_trigger_expression():
    """bucket_hour 契约：Python 查询侧与 SQL 触发器侧必须产出逐字符一致的桶字符串。"""
    conn = sqlite3.connect(":memory:")
    sql = f"SELECT {sql_hour_bucket('?')}"
    samples = [
        datetime(2026, 6, 12, 7, 34, 56, 123456, tzinfo=timezone.utc),
        datetime(2026, 6, 12, 16, 0, 0, tzinfo=timezone.utc),  # UTC+8 已跨入次日
        datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc),  # 跨年
        datetime(2026, 2, 28, 16, 30, 0, tzinfo=timezone.utc),  # 非闰年二月末
    ]
    for value in samples:
        created_at = value.isoformat()  # usage_logs.created_at 的实际写入格式（utc_now_iso）
        sql_bucket = conn.execute(sql, (created_at,)).fetchone()[0]
        assert sql_bucket == hour_bucket(value), created_at


def test_generated_scripts_are_valid_sqlite():
    """生成的触发器/重建脚本必须能在最小 schema 上完整执行。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            upstream_id TEXT,
            final_anlas_cost INTEGER,
            is_retry_success INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE dashboard_hourly_stats (
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
        CREATE TABLE dashboard_hourly_request_refs (
            bucket_hour TEXT NOT NULL,
            upstream_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            ref_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(bucket_hour, upstream_id, request_id)
        );
        """
    )
    conn.executescript(dashboard_triggers_script())
    triggers = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    assert triggers == {
        "trg_usage_logs_dashboard_insert",
        "trg_usage_logs_dashboard_update",
        "trg_usage_logs_dashboard_delete",
    }

    now = datetime(2026, 6, 12, 7, 0, 0, tzinfo=timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO usage_logs (request_id, attempt_number, status, upstream_id, final_anlas_cost, is_retry_success, created_at)"
        " VALUES ('req-1', 0, 'success', 'opus-a', 5, 0, ?)",
        (now,),
    )
    conn.execute("UPDATE usage_logs SET status = 'failed', final_anlas_cost = NULL WHERE request_id = 'req-1'")
    conn.execute("DELETE FROM usage_logs WHERE request_id = 'req-1'")
    conn.executescript(rebuild_dashboard_hourly_stats_script())
