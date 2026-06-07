from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
            count = self.db.query_one(
                """
                SELECT COUNT(DISTINCT request_id) AS c
                FROM usage_logs
                WHERE user_id = ?
                  AND created_at >= ?
                  AND status NOT IN ('rejected')
                """,
                (user_id, window_start),
            )["c"]
            max_requests = int(rule["max_requests"])
            if int(count) >= max_requests:
                return RateLimitResult(
                    allowed=False,
                    message=f"Rate limit exceeded: {max_requests} per {period}",
                    retry_after=seconds,
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
            count = self.db.query_one(
                """
                SELECT COUNT(DISTINCT l.request_id) AS c
                FROM usage_logs l
                JOIN users u ON u.id = l.user_id
                WHERE u.group_id = ?
                  AND u.deleted_at IS NULL
                  AND l.created_at >= ?
                  AND l.status NOT IN ('rejected')
                """,
                (group_id, window_start),
            )["c"]
            max_requests = int(rule["max_requests"])
            if int(count) >= max_requests:
                return RateLimitResult(
                    allowed=False,
                    message=f"Group rate limit exceeded: {max_requests} per {period}",
                    retry_after=seconds,
                    scope="group",
                )
        return RateLimitResult(allowed=True)
