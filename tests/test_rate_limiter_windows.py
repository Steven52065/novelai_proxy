from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest

from app.database import Database, utc_now_iso
import app.rate_limiter as rate_limiter_module
from app.rate_limiter import RateLimiter


FIXED_NOW = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return FIXED_NOW.replace(tzinfo=None)
        return FIXED_NOW.astimezone(tz)


@pytest.fixture
def rate_limit_db(tmp_path):
    db = Database(str(tmp_path / "rate-limit.db"))
    db.init_schema()
    try:
        yield db
    finally:
        db.close()


def _create_user(db: Database) -> int:
    cursor = db.execute(
        """
        INSERT INTO users (api_key_hash, name, tier, is_active, free_small_only, allowed_endpoints, created_at)
        VALUES (?, 'rate-user', 'normal', 1, 0, 'generate-image', ?)
        """,
        (f"hash-{uuid.uuid4().hex}", utc_now_iso()),
    )
    return int(cursor.lastrowid)


def _add_rule(db: Database, user_id: int, *, period: str, max_requests: int, active: bool = True) -> None:
    db.execute(
        """
        INSERT INTO rate_limit_rules (user_id, period, max_requests, is_active, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, period, max_requests, 1 if active else 0, utc_now_iso()),
    )


def _insert_log(db: Database, user_id: int, *, request_id: str, status: str, created_at: datetime) -> None:
    db.execute(
        """
        INSERT INTO usage_logs (request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at)
        VALUES (?, ?, 'generate', 0, ?, 'INFO', ?)
        """,
        (request_id, user_id, status, created_at.isoformat()),
    )


def test_rate_limit_counts_window_start_as_inside_period(rate_limit_db, monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    user_id = _create_user(rate_limit_db)
    _add_rule(rate_limit_db, user_id, period="minute", max_requests=1)
    _insert_log(rate_limit_db, user_id, request_id="at-boundary", status="success", created_at=FIXED_NOW - timedelta(seconds=60))

    result = RateLimiter(rate_limit_db).check(user_id)

    assert result.allowed is False
    assert result.retry_after == 60
    assert result.message == "Rate limit exceeded: 1 per minute"


def test_rate_limit_ignores_requests_before_window_start(rate_limit_db, monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    user_id = _create_user(rate_limit_db)
    _add_rule(rate_limit_db, user_id, period="minute", max_requests=1)
    _insert_log(
        rate_limit_db,
        user_id,
        request_id="before-boundary",
        status="success",
        created_at=FIXED_NOW - timedelta(seconds=60, microseconds=1),
    )

    result = RateLimiter(rate_limit_db).check(user_id)

    assert result.allowed is True


def test_rate_limit_counts_retry_attempts_once_and_ignores_rejected(rate_limit_db, monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    user_id = _create_user(rate_limit_db)
    _add_rule(rate_limit_db, user_id, period="hour", max_requests=2)
    recent = FIXED_NOW - timedelta(minutes=5)
    _insert_log(rate_limit_db, user_id, request_id="retried-request", status="failed", created_at=recent)
    rate_limit_db.execute(
        """
        INSERT INTO usage_logs (request_id, attempt_number, user_id, action, estimated_anlas_cost, status, log_level, created_at)
        VALUES (?, 1, ?, 'generate', 0, 'success', 'INFO', ?)
        """,
        ("retried-request", user_id, recent.isoformat()),
    )
    _insert_log(rate_limit_db, user_id, request_id="rejected-request", status="rejected", created_at=recent)
    _insert_log(rate_limit_db, user_id, request_id="second-real-request", status="success", created_at=recent)

    result = RateLimiter(rate_limit_db).check(user_id)

    assert result.allowed is False
    assert result.retry_after == 3600


def test_rate_limit_ignores_inactive_rules_and_applies_multiple_windows(rate_limit_db, monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    user_id = _create_user(rate_limit_db)
    _add_rule(rate_limit_db, user_id, period="minute", max_requests=1, active=False)
    _add_rule(rate_limit_db, user_id, period="day", max_requests=1)
    _insert_log(rate_limit_db, user_id, request_id="day-request", status="success", created_at=FIXED_NOW - timedelta(hours=2))

    result = RateLimiter(rate_limit_db).check(user_id)

    assert result.allowed is False
    assert result.retry_after == 86400
    assert result.message == "Rate limit exceeded: 1 per day"
