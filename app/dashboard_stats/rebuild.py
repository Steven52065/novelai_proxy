"""Full rebuild SQL for dashboard hourly statistics."""

from __future__ import annotations

from .buckets import ALL_UPSTREAMS, sql_hour_bucket
from .counters import _NOW_UTC, _STATS_INSERT_COLUMNS, _STATUS_COLUMNS, _stat_deltas


def rebuild_dashboard_hourly_stats_script() -> str:
    """Generate a script that rebuilds dashboard_hourly_* from usage_logs."""
    bucket = sql_hour_bucket("created_at")
    deltas = _stat_deltas("")
    sums = ",\n       ".join(f"SUM({deltas[column]})" for column in _STATUS_COLUMNS)
    upstream_present = "upstream_id IS NOT NULL AND upstream_id != ''"
    return f"""
DELETE FROM dashboard_hourly_stats;
DELETE FROM dashboard_hourly_request_refs;

INSERT INTO dashboard_hourly_request_refs (bucket_hour, upstream_id, request_id, ref_count)
SELECT {bucket}, '{ALL_UPSTREAMS}', request_id, COUNT(*)
FROM usage_logs
GROUP BY {bucket}, request_id;

INSERT INTO dashboard_hourly_request_refs (bucket_hour, upstream_id, request_id, ref_count)
SELECT {bucket}, upstream_id, request_id, COUNT(*)
FROM usage_logs
WHERE {upstream_present}
GROUP BY {bucket}, upstream_id, request_id;

INSERT INTO dashboard_hourly_stats ({_STATS_INSERT_COLUMNS})
SELECT {bucket},
       '{ALL_UPSTREAMS}',
       COUNT(DISTINCT request_id),
       {sums},
       {_NOW_UTC}
FROM usage_logs
GROUP BY {bucket};

INSERT INTO dashboard_hourly_stats ({_STATS_INSERT_COLUMNS})
SELECT {bucket},
       upstream_id,
       COUNT(DISTINCT request_id),
       {sums},
       {_NOW_UTC}
FROM usage_logs
WHERE {upstream_present}
GROUP BY {bucket}, upstream_id;
"""
