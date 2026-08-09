from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Literal

from .database import Database
from .rate_limit_rules import PERIOD_SECONDS

__all__ = ["PERIOD_SECONDS", "RateLimitResult", "RateLimiter"]


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    message: str = ""
    retry_after: int = 0
    scope: Literal["user", "group"] | None = None


class RateLimiter:
    def __init__(self, db: Database):
        self.db = db

    def check(self, user_id: int) -> RateLimitResult:
        rules = self.db.query_all(
            """
            SELECT id, period, max_requests
            FROM rate_limit_rules
            WHERE user_id = ? AND is_active = 1
            ORDER BY id ASC
            """,
            (user_id,),
        )
        now = datetime.now(timezone.utc)
        violations: list[RateLimitResult] = []
        for rule in rules:
            period = str(rule["period"])
            seconds = PERIOD_SECONDS.get(period)
            if not seconds:
                continue
            window_start = (now - timedelta(seconds=seconds)).isoformat()
            max_requests = int(rule["max_requests"])
            threshold_activity = self.db.query_one(
                """
                SELECT MAX(created_at) AS latest_at
                FROM usage_logs
                WHERE user_id = ?
                  AND created_at >= ?
                  AND status NOT IN ('rejected')
                GROUP BY request_id
                ORDER BY latest_at DESC
                LIMIT 1 OFFSET ?
                """,
                (user_id, window_start, max_requests - 1),
            )
            if threshold_activity is not None:
                violations.append(
                    RateLimitResult(
                        allowed=False,
                        message=f"Rate limit exceeded: {max_requests} per {period}",
                        retry_after=_retry_after_seconds(now, threshold_activity["latest_at"], seconds),
                        scope="user",
                    )
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
        if group is not None:
            group_id = int(group["id"])
            group_rules = self.db.query_all(
                """
                SELECT id, period, max_requests
                FROM group_rate_limit_rules
                WHERE group_id = ? AND is_active = 1
                ORDER BY id ASC
                """,
                (group_id,),
            )
            for rule in group_rules:
                period = str(rule["period"])
                seconds = PERIOD_SECONDS.get(period)
                if not seconds:
                    continue
                window_start = (now - timedelta(seconds=seconds)).isoformat()
                max_requests = int(rule["max_requests"])
                threshold_activity = self.db.query_one(
                    """
                    SELECT MAX(l.created_at) AS latest_at
                    FROM usage_logs l
                    JOIN users u ON u.id = l.user_id
                    WHERE u.group_id = ?
                      AND u.deleted_at IS NULL
                      AND l.created_at >= ?
                      AND l.status NOT IN ('rejected')
                    GROUP BY l.request_id
                    ORDER BY latest_at DESC
                    LIMIT 1 OFFSET ?
                    """,
                    (group_id, window_start, max_requests - 1),
                )
                if threshold_activity is not None:
                    violations.append(
                        RateLimitResult(
                            allowed=False,
                            message=f"Group rate limit exceeded: {max_requests} per {period}",
                            retry_after=_retry_after_seconds(now, threshold_activity["latest_at"], seconds),
                            scope="group",
                        )
                    )
        if not violations:
            return RateLimitResult(allowed=True)
        return max(violations, key=lambda violation: violation.retry_after)


def _retry_after_seconds(now: datetime, latest_at, window_seconds: int) -> int:
    if not latest_at:
        return window_seconds
    try:
        unlock_activity = datetime.fromisoformat(latest_at)
    except ValueError:
        return window_seconds
    if unlock_activity.tzinfo is None:
        unlock_activity = unlock_activity.replace(tzinfo=timezone.utc)
    remaining = window_seconds - (now - unlock_activity.astimezone(timezone.utc)).total_seconds()
    return max(1, math.ceil(remaining))
