from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from sqlite3 import Connection

from .daily_windows import coerce_datetime, current_window
from .database import Database, utc_now_iso


@dataclass(frozen=True)
class IdleFreeSmallDailyLimitSnapshot:
    enabled: bool
    limit: int
    used: int
    reserved: int
    available: int
    window_start: str
    reset_at: str


@dataclass(frozen=True)
class IdleFreeSmallReservation:
    user_id: int
    window_start: str
    count: int = 1
    limit: int = 0
    reset_at: str = ""


def _finite_nonnegative_decimal(value: object) -> Decimal:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("multiplier must be a finite nonnegative number") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError("multiplier must be a finite nonnegative number")
    return decimal_value


def calculate_idle_limit(base_limit: int, multiplier: object) -> int:
    decimal_multiplier = _finite_nonnegative_decimal(multiplier)
    numerator, denominator = decimal_multiplier.as_integer_ratio()
    return int(base_limit) * numerator // denominator


class IdleFreeSmallDailyLimitManager:
    def __init__(self, db: Database, *, reset_hour_utc8: int = 0):
        if not 0 <= int(reset_hour_utc8) <= 23:
            raise ValueError("reset_hour_utc8 must be between 0 and 23")
        self.db = db
        self.reset_hour_utc8 = int(reset_hour_utc8)

    def get_snapshot(self, user_id: int, *, now: datetime | None = None) -> IdleFreeSmallDailyLimitSnapshot:
        return self.get_snapshots([user_id], now=now)[user_id]

    def get_snapshots(self, user_ids: list[int], *, now: datetime | None = None) -> dict[int, IdleFreeSmallDailyLimitSnapshot]:
        user_ids = [int(user_id) for user_id in user_ids]
        snapshots: dict[int, IdleFreeSmallDailyLimitSnapshot] = {}
        if not user_ids:
            return snapshots
        now = coerce_datetime(now)
        window_start, reset_at = current_window(now, self.reset_hour_utc8)
        placeholders = ",".join("?" for _ in user_ids)
        with self.db.transaction(immediate=False) as conn:
            policy_rows = conn.execute(
                f"SELECT id, free_small_daily_limit_enabled AS enabled, free_small_daily_limit AS base_limit, idle_free_small_multiplier AS multiplier FROM users WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                tuple(user_ids),
            ).fetchall()
            usage_rows = conn.execute(
                f"SELECT user_id, used, reserved FROM idle_free_small_daily_usage WHERE user_id IN ({placeholders}) AND window_start = ?",
                (*user_ids, window_start),
            ).fetchall()
            usage_by_user = {int(row["user_id"]): (int(row["used"]), int(row["reserved"])) for row in usage_rows}
            for row in policy_rows:
                user_id = int(row["id"])
                used, reserved = usage_by_user.get(user_id, (0, 0))
                limit = 0
                if int(row["enabled"] or 0) and _finite_nonnegative_decimal(row["multiplier"]) > 0:
                    limit = calculate_idle_limit(int(row["base_limit"] or 0), row["multiplier"])
                snapshots[user_id] = _snapshot(
                    enabled=limit >= 1,
                    limit=limit,
                    used=used,
                    reserved=reserved,
                    window_start=window_start,
                    reset_at=reset_at.isoformat(),
                )
        return snapshots

    def try_reserve(self, user_id: int, *, now: datetime | None = None) -> IdleFreeSmallReservation | None:
        now = coerce_datetime(now)
        window_start, reset_at = current_window(now, self.reset_hour_utc8)
        with self.db.transaction() as conn:
            policy = self._effective_policy(conn, user_id)
            if policy is None:
                return None
            enabled, limit = policy
            if not enabled or limit < 1:
                return None
            used, reserved = self._usage_for_window(conn, user_id, window_start)
            if used + reserved + 1 > limit:
                return None
            now_iso = utc_now_iso()
            conn.execute(
                "INSERT INTO idle_free_small_daily_usage (user_id, window_start, used, reserved, created_at, updated_at) VALUES (?, ?, 0, 0, ?, ?) ON CONFLICT(user_id, window_start) DO NOTHING",
                (user_id, window_start, now_iso, now_iso),
            )
            conn.execute(
                "UPDATE idle_free_small_daily_usage SET reserved = reserved + 1, updated_at = ? WHERE user_id = ? AND window_start = ?",
                (now_iso, user_id, window_start),
            )
            return IdleFreeSmallReservation(
                user_id=user_id,
                window_start=window_start,
                limit=limit,
                reset_at=reset_at.isoformat(),
            )

    def confirm(self, reservation: IdleFreeSmallReservation | None) -> None:
        if reservation is None or reservation.count <= 0:
            return
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE idle_free_small_daily_usage SET reserved = MAX(reserved - ?, 0), used = used + ?, updated_at = ? WHERE user_id = ? AND window_start = ?",
                (reservation.count, reservation.count, utc_now_iso(), reservation.user_id, reservation.window_start),
            )

    def release(self, reservation: IdleFreeSmallReservation | None) -> None:
        if reservation is None or reservation.count <= 0:
            return
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE idle_free_small_daily_usage SET reserved = MAX(reserved - ?, 0), updated_at = ? WHERE user_id = ? AND window_start = ?",
                (reservation.count, utc_now_iso(), reservation.user_id, reservation.window_start),
            )

    def reclaim_orphan_reserved(self) -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute("UPDATE idle_free_small_daily_usage SET reserved = 0, updated_at = ? WHERE reserved != 0", (utc_now_iso(),))
            return int(cursor.rowcount)

    def _effective_policy(self, conn: Connection, user_id: int) -> tuple[bool, int] | None:
        row = conn.execute(
            "SELECT free_small_daily_limit_enabled AS enabled, free_small_daily_limit AS base_limit, idle_free_small_multiplier AS multiplier FROM users WHERE id = ? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()
        if row is None or not int(row["enabled"] or 0):
            return None
        multiplier = _finite_nonnegative_decimal(row["multiplier"])
        limit = calculate_idle_limit(int(row["base_limit"] or 0), multiplier)
        return limit >= 1, limit

    @staticmethod
    def _usage_for_window(conn: Connection, user_id: int, window_start: str) -> tuple[int, int]:
        row = conn.execute("SELECT used, reserved FROM idle_free_small_daily_usage WHERE user_id = ? AND window_start = ?", (user_id, window_start)).fetchone()
        if row is None:
            return 0, 0
        return int(row["used"]), int(row["reserved"])


def _snapshot(*, enabled: bool, limit: int, used: int, reserved: int, window_start: str, reset_at: str) -> IdleFreeSmallDailyLimitSnapshot:
    return IdleFreeSmallDailyLimitSnapshot(
        enabled=enabled,
        limit=limit,
        used=used,
        reserved=reserved,
        available=max(limit - used - reserved, 0) if enabled else 0,
        window_start=window_start,
        reset_at=reset_at,
    )
