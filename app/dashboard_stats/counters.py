"""Counter column definitions and per-log SQL delta expressions."""

from __future__ import annotations

from .buckets import ALL_UPSTREAMS

_NOW_UTC = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

# request_count is de-duplicated by request_id; status counters are per log row.
_STATUS_COLUMNS = (
    "success_count",
    "failed_count",
    "rejected_count",
    "retry_success_count",
    "anlas_cost",
)
_COUNTER_COLUMNS = ("request_count", *_STATUS_COLUMNS)
_STATS_INSERT_COLUMNS = f"bucket_hour, upstream_id, {', '.join(_COUNTER_COLUMNS)}, updated_at"


def _dimensions(row: str) -> tuple[tuple[str, str | None], ...]:
    """Return aggregate and per-upstream dimensions for a trigger row prefix."""
    return (
        (f"'{ALL_UPSTREAMS}'", None),
        (f"{row}upstream_id", f"{row}upstream_id IS NOT NULL AND {row}upstream_id != ''"),
    )


def _stat_deltas(row: str) -> dict[str, str]:
    """Return the SQL contribution of one log row to each status counter."""
    status = f"lower({row}status)"
    return {
        "success_count": f"CASE WHEN {status} = 'success' THEN 1 ELSE 0 END",
        "failed_count": f"CASE WHEN {status} = 'failed' THEN 1 ELSE 0 END",
        "rejected_count": f"CASE WHEN {status} = 'rejected' THEN 1 ELSE 0 END",
        "retry_success_count": f"CASE WHEN {status} = 'success' AND {row}is_retry_success = 1 THEN 1 ELSE 0 END",
        "anlas_cost": f"CASE WHEN {status} = 'success' THEN COALESCE({row}final_anlas_cost, 0) ELSE 0 END",
    }
