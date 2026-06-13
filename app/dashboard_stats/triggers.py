"""Trigger SQL for keeping dashboard hourly statistics in sync."""

from __future__ import annotations

from .buckets import sql_hour_bucket
from .counters import (
    _COUNTER_COLUMNS,
    _NOW_UTC,
    _STATS_INSERT_COLUMNS,
    _STATUS_COLUMNS,
    _dimensions,
    _stat_deltas,
)


def dashboard_triggers_script() -> str:
    """Generate the repeatable usage_logs -> dashboard_hourly_* trigger script."""
    apply_blocks = "".join(_apply_block(upstream, guard) for upstream, guard in _dimensions("NEW."))
    retract_blocks = "".join(_retract_block(upstream, guard) for upstream, guard in _dimensions("OLD."))
    return f"""
DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_insert;
DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_update_old;
DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_update_new;
DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_update;
DROP TRIGGER IF EXISTS trg_usage_logs_dashboard_delete;

CREATE TRIGGER trg_usage_logs_dashboard_insert
AFTER INSERT ON usage_logs
BEGIN
{apply_blocks}
END;

CREATE TRIGGER trg_usage_logs_dashboard_update
AFTER UPDATE OF created_at, request_id, upstream_id, status, is_retry_success, final_anlas_cost ON usage_logs
BEGIN
{retract_blocks}
{apply_blocks}
END;

CREATE TRIGGER trg_usage_logs_dashboard_delete
AFTER DELETE ON usage_logs
BEGIN
{retract_blocks}
END;
"""


def _apply_block(upstream: str, guard: str | None) -> str:
    """Apply NEW row deltas for one dashboard dimension."""
    bucket = sql_hour_bucket("NEW.created_at")
    deltas = _stat_deltas("NEW.")
    first_ref = f"""CASE WHEN (
            SELECT ref_count
            FROM dashboard_hourly_request_refs
            WHERE bucket_hour = {bucket}
              AND upstream_id = {upstream}
              AND request_id = NEW.request_id
        ) = 1 THEN 1 ELSE 0 END"""
    row_values = ",\n        ".join(
        [bucket, upstream, first_ref]
        + [deltas[column] for column in _STATUS_COLUMNS]
        + [_NOW_UTC]
    )
    if guard is None:
        refs_source = f"VALUES ({bucket}, {upstream}, NEW.request_id, 1)"
        stats_source = f"VALUES (\n        {row_values}\n    )"
    else:
        refs_source = f"SELECT {bucket}, {upstream}, NEW.request_id, 1\n    WHERE {guard}"
        stats_source = f"SELECT\n        {row_values}\n    WHERE {guard}"
    accumulate = ",\n        ".join(
        [f"{column} = {column} + excluded.{column}" for column in _COUNTER_COLUMNS]
        + ["updated_at = excluded.updated_at"]
    )
    return f"""
    INSERT INTO dashboard_hourly_request_refs (bucket_hour, upstream_id, request_id, ref_count)
    {refs_source}
    ON CONFLICT(bucket_hour, upstream_id, request_id)
    DO UPDATE SET ref_count = ref_count + 1;

    INSERT INTO dashboard_hourly_stats ({_STATS_INSERT_COLUMNS})
    {stats_source}
    ON CONFLICT(bucket_hour, upstream_id)
    DO UPDATE SET
        {accumulate};
"""


def _retract_block(upstream: str, guard: str | None) -> str:
    """Retract OLD row deltas for one dashboard dimension."""
    bucket = sql_hour_bucket("OLD.created_at")
    deltas = _stat_deltas("OLD.")
    guard_clause = f"\n      AND {guard}" if guard else ""
    decrements = ",\n        ".join(
        f"{column} = {column} - {deltas[column]}" for column in _STATUS_COLUMNS
    )
    return f"""
    UPDATE dashboard_hourly_stats
    SET {decrements},
        updated_at = {_NOW_UTC}
    WHERE bucket_hour = {bucket}
      AND upstream_id = {upstream}{guard_clause};

    UPDATE dashboard_hourly_request_refs
    SET ref_count = ref_count - 1
    WHERE bucket_hour = {bucket}
      AND upstream_id = {upstream}
      AND request_id = OLD.request_id{guard_clause};

    UPDATE dashboard_hourly_stats
    SET request_count = request_count - 1,
        updated_at = {_NOW_UTC}
    WHERE bucket_hour = {bucket}
      AND upstream_id = {upstream}{guard_clause}
      AND (
          SELECT ref_count
          FROM dashboard_hourly_request_refs
          WHERE bucket_hour = {bucket}
            AND upstream_id = {upstream}
            AND request_id = OLD.request_id
      ) = 0;

    DELETE FROM dashboard_hourly_request_refs
    WHERE bucket_hour = {bucket}
      AND upstream_id = {upstream}
      AND request_id = OLD.request_id{guard_clause}
      AND ref_count <= 0;
"""
