from __future__ import annotations

from datetime import datetime


def _dashboard_hourly_totals(db, upstream_id: str) -> dict[str, int]:
    row = db.query_one(
        """
        SELECT COALESCE(SUM(request_count), 0) AS requests,
               COALESCE(SUM(success_count), 0) AS success,
               COALESCE(SUM(failed_count), 0) AS failed,
               COALESCE(SUM(rejected_count), 0) AS rejected,
               COALESCE(SUM(retry_success_count), 0) AS retry_success,
               COALESCE(SUM(anlas_cost), 0) AS anlas
        FROM dashboard_hourly_stats
        WHERE upstream_id = ?
        """,
        (upstream_id,),
    )
    return {key: int(row[key] or 0) for key in row.keys()}


def _unique_request_count(attempts: list[tuple[str, int, str, int, datetime]], start: datetime, end: datetime) -> int:
    return len({request_id for request_id, _, _, _, created_at in attempts if start <= created_at < end})


