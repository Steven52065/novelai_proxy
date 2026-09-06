from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .timezones import DISPLAY_TIMEZONE as UTC8


def coerce_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def current_window(now: datetime, reset_hour_utc8: int) -> tuple[str, datetime]:
    local_now = coerce_datetime(now).astimezone(UTC8)
    window_start = local_now.replace(hour=reset_hour_utc8, minute=0, second=0, microsecond=0)
    if local_now < window_start:
        window_start -= timedelta(days=1)
    reset_at = window_start + timedelta(days=1)
    return window_start.isoformat(), reset_at


def retry_after_seconds(now: datetime, reset_at: datetime) -> int:
    seconds = (reset_at.astimezone(timezone.utc) - coerce_datetime(now)).total_seconds()
    return max(0, int(math.ceil(seconds)))


def seconds_until(reset_at: str, now: datetime) -> int:
    try:
        parsed = datetime.fromisoformat(reset_at)
    except ValueError:
        return 0
    return retry_after_seconds(now, parsed)
