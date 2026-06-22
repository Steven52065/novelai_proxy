from __future__ import annotations

from datetime import datetime, timezone

from app.database_maintenance import next_daily_run_utc8
from app.timezones import DISPLAY_TIMEZONE


def test_next_daily_run_utc8_uses_same_day_before_target_time():
    now = datetime(2026, 6, 22, 3, 59, tzinfo=DISPLAY_TIMEZONE)
    next_run = next_daily_run_utc8(now.astimezone(timezone.utc), "04:00")

    assert next_run.astimezone(DISPLAY_TIMEZONE) == datetime(2026, 6, 22, 4, 0, tzinfo=DISPLAY_TIMEZONE)


def test_next_daily_run_utc8_uses_next_day_at_or_after_target_time():
    now = datetime(2026, 6, 22, 4, 0, tzinfo=DISPLAY_TIMEZONE)
    next_run = next_daily_run_utc8(now.astimezone(timezone.utc), "04:00")

    assert next_run.astimezone(DISPLAY_TIMEZONE) == datetime(2026, 6, 23, 4, 0, tzinfo=DISPLAY_TIMEZONE)
