from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from sqlite3 import Connection

from .database import Database, utc_now_iso
from .timezones import DISPLAY_TIMEZONE


@dataclass(frozen=True)
class QuotaSnapshot:
    total: int
    used: int
    reserved: int

    @property
    def available(self) -> int:
        return max(self.total - self.used - self.reserved, 0)


class InsufficientQuota(Exception):
    def __init__(self, need: int, have: int):
        self.need = need
        self.have = have
        super().__init__(f"anlas 额度不足：需要 {need}，可用 {have}")


class QuotaManager:
    def __init__(self, db: Database):
        self.db = db

    def get_snapshot(self, user_id: int) -> QuotaSnapshot:
        self.reset_if_due(user_id)
        row = self.db.query_one(
            "SELECT total, used, reserved FROM user_anlas_quota WHERE user_id = ?",
            (user_id,),
        )
        if row is None:
            return QuotaSnapshot(total=0, used=0, reserved=0)
        return QuotaSnapshot(total=int(row["total"]), used=int(row["used"]), reserved=int(row["reserved"]))

    def reserve(self, user_id: int, cost: int) -> QuotaSnapshot:
        self.reset_if_due(user_id)
        if cost <= 0:
            return self.get_snapshot(user_id)
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT total, used, reserved FROM user_anlas_quota WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise InsufficientQuota(need=cost, have=0)
            available = int(row["total"]) - int(row["used"]) - int(row["reserved"])
            if available < cost:
                raise InsufficientQuota(need=cost, have=max(available, 0))
            conn.execute(
                "UPDATE user_anlas_quota SET reserved = reserved + ? WHERE user_id = ?",
                (cost, user_id),
            )
            return QuotaSnapshot(
                total=int(row["total"]),
                used=int(row["used"]),
                reserved=int(row["reserved"]) + cost,
            )

    def confirm(self, user_id: int, cost: int) -> None:
        if cost <= 0:
            return
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE user_anlas_quota
                SET reserved = MAX(reserved - ?, 0),
                    used = used + ?
                WHERE user_id = ?
                """,
                (cost, cost, user_id),
            )

    def release(self, user_id: int, cost: int) -> None:
        if cost <= 0:
            return
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE user_anlas_quota SET reserved = MAX(reserved - ?, 0) WHERE user_id = ?",
                (cost, user_id),
            )

    def reclaim_orphan_reserved(self) -> int:
        # Only safe for this project's single-process deployment: the in-memory queue
        # is empty after a restart, so no persisted reservation can still be in flight.
        with self.db.transaction() as conn:
            cursor = conn.execute("UPDATE user_anlas_quota SET reserved = 0 WHERE reserved != 0")
            return int(cursor.rowcount)

    def create_or_update(self, user_id: int, total: int, reset_period: str = "month", reset_day: int | None = None) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            self.create_or_update_with_connection(conn, user_id, total, reset_period, reset_day, now=now)

    def create_or_update_with_connection(
        self,
        conn: Connection,
        user_id: int,
        total: int,
        reset_period: str = "month",
        reset_day: int | None = None,
        *,
        now: str | None = None,
    ) -> None:
        now = now or utc_now_iso()
        reset_day = normalize_reset_day(reset_period, reset_day, _parse_iso(now))
        conn.execute(
            """
            INSERT INTO user_anlas_quota
                (user_id, total, used, reserved, reset_period, reset_day, last_reset_at, created_at)
            VALUES (?, ?, 0, 0, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                total = excluded.total,
                reset_period = excluded.reset_period,
                reset_day = excluded.reset_day
            """,
            (user_id, total, reset_period, reset_day, now, now),
        )

    def reset_usage(self, user_id: int, *, update_last_reset_at: bool = True) -> None:
        # update_last_reset_at=False 表示周期外的额外重置：只清零用量，
        # 保留 last_reset_at，使下一次自动重置时间不受影响。
        with self.db.transaction() as conn:
            if update_last_reset_at:
                conn.execute(
                    """
                    UPDATE user_anlas_quota
                    SET used = 0, reserved = 0, last_reset_at = ?
                    WHERE user_id = ?
                    """,
                    (utc_now_iso(), user_id),
                )
            else:
                conn.execute(
                    "UPDATE user_anlas_quota SET used = 0, reserved = 0 WHERE user_id = ?",
                    (user_id,),
                )

    def reset_if_due(self, user_id: int) -> bool:
        row = self.db.query_one(
            "SELECT reset_period, reset_day, last_reset_at, created_at FROM user_anlas_quota WHERE user_id = ?",
            (user_id,),
        )
        if row is None:
            return False
        period = str(row["reset_period"])
        if period == "never":
            return False
        last_reset_at = _parse_iso(row["last_reset_at"] or row["created_at"])
        now = datetime.now(timezone.utc)
        if now < _next_reset_at(last_reset_at, period, int(row["reset_day"])):
            return False
        self.reset_usage(user_id)
        return True

def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _next_reset_at(last_reset_at: datetime, period: str, reset_day: int) -> datetime:
    if period == "day":
        return last_reset_at + timedelta(days=1)
    local_last_reset_at = last_reset_at.astimezone(DISPLAY_TIMEZONE)
    if period == "week":
        days = max(1, reset_day) - 1
        start = local_last_reset_at + timedelta(days=1)
        candidate = start + timedelta(days=(days - start.weekday()) % 7)
        local_candidate = candidate.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_candidate.astimezone(timezone.utc)
    if period == "month":
        month = local_last_reset_at.month + 1
        year = local_last_reset_at.year
        if month == 13:
            month = 1
            year += 1
        day = max(1, min(reset_day, 28))
        local_candidate = local_last_reset_at.replace(
            year=year,
            month=month,
            day=day,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return local_candidate.astimezone(timezone.utc)
    return datetime.max.replace(tzinfo=timezone.utc)


def normalize_reset_day(period: str, reset_day: int | None, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(DISPLAY_TIMEZONE)
    if period == "month":
        day = min(local_now.day, 28) if reset_day is None else int(reset_day)
        if not 1 <= day <= 28:
            raise ValueError("reset_period 为 month 时 reset_day 必须在 1 到 28 之间")
        return day
    if period == "week":
        day = local_now.weekday() + 1 if reset_day is None else int(reset_day)
        if not 1 <= day <= 7:
            raise ValueError("reset_period 为 week 时 reset_day 必须在 1 到 7 之间")
        return day
    if period in {"day", "never"}:
        return 0
    raise ValueError(f"不支持的 reset_period：{period}")
