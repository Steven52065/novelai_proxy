"""Shared hour-bucket contract for dashboard hourly statistics."""

from __future__ import annotations

from datetime import datetime

from ..timezones import DISPLAY_TIMEZONE, TZ_OFFSET_HOURS

# Reserved upstream id for aggregate rows. Real upstream ids must not use it.
ALL_UPSTREAMS = "__all__"

# Keep this to directives supported identically by Python strftime and SQLite strftime.
BUCKET_HOUR_FORMAT = f"%Y-%m-%dT%H:00:00+{TZ_OFFSET_HOURS:02d}:00"


def hour_bucket(value: datetime) -> str:
    """Return the dashboard hour bucket string used by Python-side queries."""
    local = value.astimezone(DISPLAY_TIMEZONE).replace(minute=0, second=0, microsecond=0)
    return local.strftime(BUCKET_HOUR_FORMAT)


def sql_hour_bucket(column_expr: str) -> str:
    """Return the SQLite expression that produces the same bucket as hour_bucket()."""
    return f"strftime('{BUCKET_HOUR_FORMAT}', datetime({column_expr}, '{TZ_OFFSET_HOURS:+d} hours'))"
