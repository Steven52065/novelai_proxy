from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest

from app.database import Database, utc_now_iso
from app.rate_limit_rules import RateLimitRule, RateLimitRuleSet
import app.rate_limiter as rate_limiter_module
from app.rate_limiter import RateLimiter
from app.users import replace_user_rate_limit_rules_with_connection


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


def _create_user(db: Database, group_id: int | None = None) -> int:
    cursor = db.execute(
        """
        INSERT INTO users (api_key_hash, name, tier, is_active, free_small_only, allowed_endpoints, group_id, created_at)
        VALUES (?, 'rate-user', 'normal', 1, 0, 'generate-image', ?, ?)
        """,
        (f"hash-{uuid.uuid4().hex}", group_id, utc_now_iso()),
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


def _create_group(db: Database, *, active: bool = True) -> int:
    cursor = db.execute(
        """
        INSERT INTO user_groups(name, is_active, created_at)
        VALUES (?, ?, ?)
        """,
        (f"group-{uuid.uuid4().hex}", 1 if active else 0, utc_now_iso()),
    )
    return int(cursor.lastrowid)


def _add_group_rule(db: Database, group_id: int, *, period: str, max_requests: int, active: bool = True) -> None:
    db.execute(
        """
        INSERT INTO group_rate_limit_rules (group_id, period, max_requests, is_active, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (group_id, period, max_requests, 1 if active else 0, utc_now_iso()),
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
    assert result.retry_after == 1
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
    assert result.retry_after == 3300


def test_rate_limit_ignores_inactive_rules_and_applies_multiple_windows(rate_limit_db, monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    user_id = _create_user(rate_limit_db)
    _add_rule(rate_limit_db, user_id, period="minute", max_requests=1, active=False)
    _add_rule(rate_limit_db, user_id, period="day", max_requests=1)
    _insert_log(rate_limit_db, user_id, request_id="day-request", status="success", created_at=FIXED_NOW - timedelta(hours=2))

    result = RateLimiter(rate_limit_db).check(user_id)

    assert result.allowed is False
    assert result.retry_after == 22 * 3600
    assert result.message == "Rate limit exceeded: 1 per day"


def test_group_rate_limit_counts_group_members_once_and_ignores_rejected(rate_limit_db, monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    group_id = _create_group(rate_limit_db)
    first_user = _create_user(rate_limit_db, group_id=group_id)
    second_user = _create_user(rate_limit_db, group_id=group_id)
    _add_group_rule(rate_limit_db, group_id, period="minute", max_requests=2)
    recent = FIXED_NOW - timedelta(seconds=10)
    _insert_log(rate_limit_db, first_user, request_id="first-request", status="success", created_at=recent)
    _insert_log(rate_limit_db, second_user, request_id="rejected-request", status="rejected", created_at=recent)
    _insert_log(rate_limit_db, first_user, request_id="retried-request", status="failed", created_at=recent)
    rate_limit_db.execute(
        """
        INSERT INTO usage_logs (request_id, attempt_number, user_id, action, estimated_anlas_cost, status, log_level, created_at)
        VALUES (?, 1, ?, 'generate', 0, 'success', 'INFO', ?)
        """,
        ("retried-request", first_user, recent.isoformat()),
    )

    result = RateLimiter(rate_limit_db).check(second_user)

    assert result.allowed is False
    assert result.scope == "group"
    assert result.retry_after == 50
    assert result.message == "Group rate limit exceeded: 2 per minute"


def test_group_rate_limit_does_not_apply_without_active_group(rate_limit_db, monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    disabled_group = _create_group(rate_limit_db, active=False)
    disabled_group_user = _create_user(rate_limit_db, group_id=disabled_group)
    no_group_user = _create_user(rate_limit_db)
    _add_group_rule(rate_limit_db, disabled_group, period="minute", max_requests=1)
    _insert_log(
        rate_limit_db,
        disabled_group_user,
        request_id="disabled-group-request",
        status="success",
        created_at=FIXED_NOW - timedelta(seconds=10),
    )

    assert RateLimiter(rate_limit_db).check(disabled_group_user).allowed is True
    assert RateLimiter(rate_limit_db).check(no_group_user).allowed is True


def test_month_rate_limit_is_a_rolling_thirty_day_window(rate_limit_db, monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    user_id = _create_user(rate_limit_db)
    _add_rule(rate_limit_db, user_id, period="month", max_requests=1)
    _insert_log(
        rate_limit_db,
        user_id,
        request_id="rolling-month-request",
        status="success",
        created_at=FIXED_NOW - timedelta(days=29, hours=23),
    )

    result = RateLimiter(rate_limit_db).check(user_id)

    assert result.allowed is False
    assert result.retry_after == 3600


def test_rate_limit_waits_until_count_falls_below_limit(rate_limit_db, monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    user_id = _create_user(rate_limit_db)
    _add_rule(rate_limit_db, user_id, period="minute", max_requests=2)
    _insert_log(rate_limit_db, user_id, request_id="first", status="success", created_at=FIXED_NOW - timedelta(seconds=30))
    _insert_log(rate_limit_db, user_id, request_id="second", status="success", created_at=FIXED_NOW - timedelta(seconds=20))
    _insert_log(rate_limit_db, user_id, request_id="third", status="success", created_at=FIXED_NOW - timedelta(seconds=10))

    result = RateLimiter(rate_limit_db).check(user_id)

    assert result.allowed is False
    assert result.retry_after == 40


def test_rate_limit_uses_latest_attempt_for_each_request(rate_limit_db, monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    user_id = _create_user(rate_limit_db)
    _add_rule(rate_limit_db, user_id, period="minute", max_requests=1)
    _insert_log(rate_limit_db, user_id, request_id="retried", status="failed", created_at=FIXED_NOW - timedelta(seconds=50))
    rate_limit_db.execute(
        """
        INSERT INTO usage_logs (request_id, attempt_number, user_id, action, estimated_anlas_cost, status, log_level, created_at)
        VALUES (?, 1, ?, 'generate', 0, 'success', 'INFO', ?)
        """,
        ("retried", user_id, (FIXED_NOW - timedelta(seconds=10)).isoformat()),
    )

    result = RateLimiter(rate_limit_db).check(user_id)

    assert result.allowed is False
    assert result.retry_after == 50


def test_group_rate_limit_waits_until_group_count_falls_below_limit(rate_limit_db, monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    group_id = _create_group(rate_limit_db)
    first_user = _create_user(rate_limit_db, group_id=group_id)
    second_user = _create_user(rate_limit_db, group_id=group_id)
    _add_group_rule(rate_limit_db, group_id, period="minute", max_requests=2)
    _insert_log(rate_limit_db, first_user, request_id="first", status="success", created_at=FIXED_NOW - timedelta(seconds=30))
    _insert_log(rate_limit_db, second_user, request_id="second", status="success", created_at=FIXED_NOW - timedelta(seconds=20))
    _insert_log(rate_limit_db, first_user, request_id="third", status="success", created_at=FIXED_NOW - timedelta(seconds=10))

    result = RateLimiter(rate_limit_db).check(second_user)

    assert result.allowed is False
    assert result.scope == "group"
    assert result.retry_after == 40


def test_rate_limit_returns_longest_wait_across_user_rules(rate_limit_db, monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    user_id = _create_user(rate_limit_db)
    _add_rule(rate_limit_db, user_id, period="minute", max_requests=1)
    _add_rule(rate_limit_db, user_id, period="day", max_requests=1)
    _insert_log(
        rate_limit_db,
        user_id,
        request_id="shared-user-window",
        status="success",
        created_at=FIXED_NOW - timedelta(seconds=30),
    )

    result = RateLimiter(rate_limit_db).check(user_id)

    assert result.allowed is False
    assert result.scope == "user"
    assert result.message == "Rate limit exceeded: 1 per day"
    assert result.retry_after == 86400 - 30


def test_rate_limit_returns_longest_wait_across_user_and_group_rules(rate_limit_db, monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    group_id = _create_group(rate_limit_db)
    user_id = _create_user(rate_limit_db, group_id=group_id)
    _add_rule(rate_limit_db, user_id, period="minute", max_requests=1)
    _add_group_rule(rate_limit_db, group_id, period="hour", max_requests=1)
    _insert_log(
        rate_limit_db,
        user_id,
        request_id="shared-scope-window",
        status="success",
        created_at=FIXED_NOW - timedelta(seconds=30),
    )

    result = RateLimiter(rate_limit_db).check(user_id)

    assert result.allowed is False
    assert result.scope == "group"
    assert result.message == "Group rate limit exceeded: 1 per hour"
    assert result.retry_after == 3600 - 30


def test_rate_limit_prefers_user_rule_when_waits_are_equal(rate_limit_db, monkeypatch):
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    group_id = _create_group(rate_limit_db)
    user_id = _create_user(rate_limit_db, group_id=group_id)
    _add_rule(rate_limit_db, user_id, period="minute", max_requests=1)
    _add_group_rule(rate_limit_db, group_id, period="minute", max_requests=1)
    _insert_log(
        rate_limit_db,
        user_id,
        request_id="equal-scope-window",
        status="success",
        created_at=FIXED_NOW - timedelta(seconds=30),
    )

    result = RateLimiter(rate_limit_db).check(user_id)

    assert result.allowed is False
    assert result.scope == "user"
    assert result.retry_after == 30


def test_group_member_rules_expand_to_independent_per_user_counters(rate_limit_db, monkeypatch):
    """组模板展开成每人各自的规则，一人超限不影响同组其他成员。

    这是与 group_rate_limit_rules（全组共用一个计数池）的关键区别。
    """
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    group_id = _create_group(rate_limit_db)
    first_id = _create_user(rate_limit_db, group_id=group_id)
    second_id = _create_user(rate_limit_db, group_id=group_id)
    rules = RateLimitRuleSet.of([RateLimitRule(period="minute", max_requests=1)])
    with rate_limit_db.transaction() as conn:
        replace_user_rate_limit_rules_with_connection(conn, first_id, rules)
        replace_user_rate_limit_rules_with_connection(conn, second_id, rules)
    _insert_log(
        rate_limit_db,
        first_id,
        request_id="member-independent",
        status="success",
        created_at=FIXED_NOW - timedelta(seconds=30),
    )

    limiter = RateLimiter(rate_limit_db)
    first_result = limiter.check(first_id)
    second_result = limiter.check(second_id)

    assert first_result.allowed is False
    assert first_result.scope == "user"
    assert first_result.retry_after == 30
    assert second_result.allowed is True


def test_group_shared_rule_still_pools_all_members(rate_limit_db, monkeypatch):
    """对照组：group_rate_limit_rules 仍按全组聚合，一人用满则全组被挡。"""
    monkeypatch.setattr(rate_limiter_module, "datetime", FrozenDateTime)
    group_id = _create_group(rate_limit_db)
    first_id = _create_user(rate_limit_db, group_id=group_id)
    second_id = _create_user(rate_limit_db, group_id=group_id)
    _add_group_rule(rate_limit_db, group_id, period="minute", max_requests=1)
    _insert_log(
        rate_limit_db,
        first_id,
        request_id="shared-pool",
        status="success",
        created_at=FIXED_NOW - timedelta(seconds=30),
    )

    result = RateLimiter(rate_limit_db).check(second_id)

    assert result.allowed is False
    assert result.scope == "group"
