from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Literal

from .database import Database


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    message: str = ""
    retry_after: int = 0
    scope: Literal["user", "group"] | None = None


PERIOD_SECONDS = {
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "month": 30 * 86400,
}


class RateLimiter:
    def __init__(self, db: Database):
        self.db = db

    def check(self, user_id: int) -> RateLimitResult:
        rules = self.db.query_all(
            """
            SELECT period, max_requests
            FROM rate_limit_rules
            WHERE user_id = ? AND is_active = 1
            """,
            (user_id,),
        )
        now = datetime.now(timezone.utc)
        for rule in rules:
            period = str(rule["period"])
            seconds = PERIOD_SECONDS.get(period)
            if not seconds:
                continue
            window_start = (now - timedelta(seconds=seconds)).isoformat()
            usage = self.db.query_one(
                """
                SELECT COUNT(DISTINCT request_id) AS c, MIN(created_at) AS earliest_at
                FROM usage_logs
                WHERE user_id = ?
                  AND created_at >= ?
                  AND status NOT IN ('rejected')
                """,
                (user_id, window_start),
            )
            count = usage["c"]
            max_requests = int(rule["max_requests"])
            if int(count) >= max_requests:
                return RateLimitResult(
                    allowed=False,
                    message=f"Rate limit exceeded: {max_requests} per {period}",
                    retry_after=_remaining_window_seconds(now, usage["earliest_at"], seconds),
                    scope="user",
                )
        group = self.db.query_one(
            """
            SELECT g.id
            FROM users u
            JOIN user_groups g ON g.id = u.group_id
            WHERE u.id = ?
              AND u.deleted_at IS NULL
              AND g.is_active = 1
            """,
            (user_id,),
        )
        if group is None:
            return RateLimitResult(allowed=True)

        group_id = int(group["id"])
        group_rules = self.db.query_all(
            """
            SELECT period, max_requests
            FROM group_rate_limit_rules
            WHERE group_id = ? AND is_active = 1
            """,
            (group_id,),
        )
        for rule in group_rules:
            period = str(rule["period"])
            seconds = PERIOD_SECONDS.get(period)
            if not seconds:
                continue
            window_start = (now - timedelta(seconds=seconds)).isoformat()
            usage = self.db.query_one(
                """
                SELECT COUNT(DISTINCT l.request_id) AS c, MIN(l.created_at) AS earliest_at
                FROM usage_logs l
                JOIN users u ON u.id = l.user_id
                WHERE u.group_id = ?
                  AND u.deleted_at IS NULL
                  AND l.created_at >= ?
                  AND l.status NOT IN ('rejected')
                """,
                (group_id, window_start),
            )
            count = usage["c"]
            max_requests = int(rule["max_requests"])
            if int(count) >= max_requests:
                return RateLimitResult(
                    allowed=False,
                    message=f"Group rate limit exceeded: {max_requests} per {period}",
                    retry_after=_remaining_window_seconds(now, usage["earliest_at"], seconds),
                    scope="group",
                )
        return RateLimitResult(allowed=True)


def _remaining_window_seconds(now: datetime, earliest_at: str | None, window_seconds: int) -> int:
    if not earliest_at:
        return window_seconds
    try:
        earliest = datetime.fromisoformat(earliest_at)
    except ValueError:
        return window_seconds
    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=timezone.utc)
    remaining = window_seconds - (now - earliest.astimezone(timezone.utc)).total_seconds()
    return max(1, math.ceil(remaining))
