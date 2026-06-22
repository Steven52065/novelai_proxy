from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from .config import AutoVacuumConfig
from .database import Database
from .logging_utils import logger
from .timezones import DISPLAY_TIMEZONE


def next_daily_run_utc8(now: datetime, run_time_utc8: str) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    hour, minute = _parse_run_time(run_time_utc8)
    local_now = now.astimezone(DISPLAY_TIMEZONE)
    local_target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local_target <= local_now:
        local_target += timedelta(days=1)
    return local_target.astimezone(timezone.utc)


async def auto_vacuum_loop(db: Database, config: AutoVacuumConfig) -> None:
    while True:
        next_run = next_daily_run_utc8(datetime.now(timezone.utc), config.run_time_utc8)
        delay_seconds = max((next_run - datetime.now(timezone.utc)).total_seconds(), 0)
        logger.info(
            "database auto vacuum scheduled run_time_utc8=%s next_run=%s",
            config.run_time_utc8,
            next_run.isoformat(),
        )
        await asyncio.sleep(delay_seconds)
        vacuum_task = asyncio.create_task(asyncio.to_thread(db.vacuum))
        try:
            result = await asyncio.shield(vacuum_task)
        except asyncio.CancelledError:
            try:
                await vacuum_task
            except Exception:
                logger.exception("database auto vacuum failed during shutdown")
            raise
        except Exception:
            logger.exception("database auto vacuum failed")
        else:
            logger.info(
                "database auto vacuum completed before_bytes=%s after_bytes=%s reclaimed_bytes=%s",
                result["before_bytes"],
                result["after_bytes"],
                result["reclaimed_bytes"],
            )


def _parse_run_time(value: str) -> tuple[int, int]:
    hour_text, minute_text = value.split(":", maxsplit=1)
    return int(hour_text), int(minute_text)
