from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from sqlite3 import Connection

from .database import Database, utc_now_iso
from .timezones import DISPLAY_TIMEZONE as UTC8


@dataclass(frozen=True)
class FreeSmallDailyLimitSnapshot:
    enabled: bool
    limit: int
    scope: str | None
    used: int
    reserved: int
    available: int | None
    window_start: str
    reset_at: str


@dataclass(frozen=True)
class FreeSmallDailyReservation:
    user_id: int
    window_start: str
    count: int
    scope: str
    limit: int
    reset_at: str


class FreeSmallDailyLimitExceeded(Exception):
    def __init__(self, *, snapshot: FreeSmallDailyLimitSnapshot, requested: int, retry_after: int):
        self.snapshot = snapshot
        self.requested = requested
        self.retry_after = retry_after
        self.remaining = max(snapshot.limit - snapshot.used - snapshot.reserved, 0)
        super().__init__(f"免费小图每日限额已用尽：每日最多 {snapshot.limit} 次")


class FreeSmallDailyLimitManager:
    def __init__(self, db: Database, *, reset_hour_utc8: int = 0):
        if not 0 <= int(reset_hour_utc8) <= 23:
            raise ValueError("reset_hour_utc8 must be between 0 and 23")
        self.db = db
        self.reset_hour_utc8 = int(reset_hour_utc8)

    def get_snapshot(self, user_id: int, *, now: datetime | None = None) -> FreeSmallDailyLimitSnapshot:
        now = _coerce_datetime(now)
        window_start, reset_at = _current_window(now, self.reset_hour_utc8)
        with self.db.transaction(immediate=False) as conn:
            effective = self._effective_limit(conn, user_id)
            used, reserved = self._usage_for_window(conn, user_id, window_start)
        return _snapshot(
            enabled=effective is not None,
            limit=0 if effective is None else effective.limit,
            scope=None if effective is None else effective.scope,
            used=used,
            reserved=reserved,
            window_start=window_start,
            reset_at=reset_at.isoformat(),
        )

    def reserve(self, user_id: int, count: int, *, now: datetime | None = None) -> FreeSmallDailyReservation | None:
        count = int(count)
        if count <= 0:
            return None

        now = _coerce_datetime(now)
        window_start, reset_at = _current_window(now, self.reset_hour_utc8)
        retry_after = _retry_after_seconds(now, reset_at)
        with self.db.transaction() as conn:
            effective = self._effective_limit(conn, user_id)
            if effective is None:
                return None

            used, reserved = self._usage_for_window(conn, user_id, window_start)
            current = _snapshot(
                enabled=True,
                limit=effective.limit,
                scope=effective.scope,
                used=used,
                reserved=reserved,
                window_start=window_start,
                reset_at=reset_at.isoformat(),
            )
            if used + reserved + count > effective.limit:
                raise FreeSmallDailyLimitExceeded(
                    snapshot=current,
                    requested=count,
                    retry_after=retry_after,
                )

            now_iso = utc_now_iso()
            conn.execute(
                """
                INSERT INTO free_small_daily_usage (user_id, window_start, used, reserved, created_at, updated_at)
                VALUES (?, ?, 0, 0, ?, ?)
                ON CONFLICT(user_id, window_start) DO NOTHING
                """,
                (user_id, window_start, now_iso, now_iso),
            )
            conn.execute(
                """
                UPDATE free_small_daily_usage
                SET reserved = reserved + ?,
                    updated_at = ?
                WHERE user_id = ? AND window_start = ?
                """,
                (count, now_iso, user_id, window_start),
            )
            return FreeSmallDailyReservation(
                user_id=user_id,
                window_start=window_start,
                count=count,
                scope=effective.scope,
                limit=effective.limit,
                reset_at=reset_at.isoformat(),
            )

    def confirm(self, reservation: FreeSmallDailyReservation | None) -> None:
        if reservation is None or reservation.count <= 0:
            return
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE free_small_daily_usage
                SET reserved = MAX(reserved - ?, 0),
                    used = used + ?,
                    updated_at = ?
                WHERE user_id = ? AND window_start = ?
                """,
                (
                    reservation.count,
                    reservation.count,
                    utc_now_iso(),
                    reservation.user_id,
                    reservation.window_start,
                ),
            )

    def release(self, reservation: FreeSmallDailyReservation | None) -> None:
        if reservation is None or reservation.count <= 0:
            return
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE free_small_daily_usage
                SET reserved = MAX(reserved - ?, 0),
                    updated_at = ?
                WHERE user_id = ? AND window_start = ?
                """,
                (
                    reservation.count,
                    utc_now_iso(),
                    reservation.user_id,
                    reservation.window_start,
                ),
            )

    def reclaim_orphan_reserved(self) -> int:
        # Only safe for this project's single-process deployment: the in-memory queue
        # is empty after a restart, so no persisted reservation can still be in flight.
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE free_small_daily_usage
                SET reserved = 0,
                    updated_at = ?
                WHERE reserved != 0
                """,
                (utc_now_iso(),),
            )
            return int(cursor.rowcount)

    def reset_usage(self, user_id: int, *, now: datetime | None = None) -> None:
        # 管理员手动重置：只清零当前窗口的用量，窗口边界由时钟决定，
        # 因此每日自动重置周期不受影响。
        now = _coerce_datetime(now)
        window_start, _ = _current_window(now, self.reset_hour_utc8)
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE free_small_daily_usage
                SET used = 0, reserved = 0, updated_at = ?
                WHERE user_id = ? AND window_start = ?
                """,
                (utc_now_iso(), user_id, window_start),
            )

    def _effective_limit(self, conn: Connection, user_id: int) -> _EffectiveLimit | None:
        # 用户组的每日限制只作为批量写入用户配置的默认值，运行时仅以用户自身配置为准。
        row = conn.execute(
            """
            SELECT free_small_daily_limit_enabled AS enabled,
                   free_small_daily_limit AS limit_value
            FROM users
            WHERE id = ? AND deleted_at IS NULL
            """,
            (user_id,),
        ).fetchone()
        if row is None or not int(row["enabled"] or 0):
            return None
        return _EffectiveLimit(scope="user", limit=int(row["limit_value"] or 0))

    @staticmethod
    def _usage_for_window(conn: Connection, user_id: int, window_start: str) -> tuple[int, int]:
        row = conn.execute(
            """
            SELECT used, reserved
            FROM free_small_daily_usage
            WHERE user_id = ? AND window_start = ?
            """,
            (user_id, window_start),
        ).fetchone()
        if row is None:
            return 0, 0
        return int(row["used"]), int(row["reserved"])


@dataclass(frozen=True)
class _EffectiveLimit:
    scope: str
    limit: int


def _snapshot(
    *,
    enabled: bool,
    limit: int,
    scope: str | None,
    used: int,
    reserved: int,
    window_start: str,
    reset_at: str,
) -> FreeSmallDailyLimitSnapshot:
    return FreeSmallDailyLimitSnapshot(
        enabled=enabled,
        limit=limit,
        scope=scope,
        used=used,
        reserved=reserved,
        available=max(limit - used - reserved, 0) if enabled else None,
        window_start=window_start,
        reset_at=reset_at,
    )


def _coerce_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _current_window(now: datetime, reset_hour_utc8: int) -> tuple[str, datetime]:
    local_now = now.astimezone(UTC8)
    window_start = local_now.replace(hour=reset_hour_utc8, minute=0, second=0, microsecond=0)
    if local_now < window_start:
        window_start -= timedelta(days=1)
    reset_at = window_start + timedelta(days=1)
    return window_start.isoformat(), reset_at


def _retry_after_seconds(now: datetime, reset_at: datetime) -> int:
    seconds = (reset_at.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    return max(0, int(math.ceil(seconds)))
