from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .database import Database


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    message: str = ""
    retry_after: int = 0


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
                )
        return RateLimitResult(allowed=True)
