from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.database import Database, utc_now_iso
from app.idle_free_small_daily_limit import (
    IdleFreeSmallDailyLimitManager,
    calculate_idle_limit,
)


@pytest.mark.parametrize(
    "base,multiplier,expected",
    [
        (100, 0.29, 29),
        (10, 0.5, 5),
        (3, 0.5, 1),
        (1, 0.5, 0),
        (0, 0.5, 0),
        (100, 0, 0),
    ],
)
def test_calculate_idle_limit_uses_decimal_floor(base: int, multiplier: float, expected: int):
    assert calculate_idle_limit(base, multiplier) == expected


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), float("-inf")])
def test_calculate_idle_limit_rejects_non_finite_or_negative(value: float):
    with pytest.raises(ValueError):
        calculate_idle_limit(10, value)


def test_idle_limit_reserves_confirms_and_counts(tmp_path: Path):
    db = _db(tmp_path)
    user_id = _create_user(db, enabled=True, limit=4, multiplier=0.5)
    manager = IdleFreeSmallDailyLimitManager(db)
    now = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)

    snapshot = manager.get_snapshot(user_id, now=now)
    assert snapshot.enabled is True
    assert snapshot.limit == 2
    assert snapshot.used == 0
    assert snapshot.reserved == 0
    assert snapshot.available == 2

    first = manager.try_reserve(user_id, now=now)
    second = manager.try_reserve(user_id, now=now)
    assert first is not None and second is not None
    assert manager.try_reserve(user_id, now=now) is None

    manager.confirm(first)
    manager.release(second)
    snapshot = manager.get_snapshot(user_id, now=now)
    assert snapshot.used == 1
    assert snapshot.reserved == 0
    assert snapshot.available == 1
    db.close()


def test_idle_limit_disabled_returns_none_and_keeps_usage_visible(tmp_path: Path):
    db = _db(tmp_path)
    disabled_multiplier = _create_user(db, enabled=True, limit=10, multiplier=0)
    disabled_daily = _create_user(db, enabled=False, limit=10, multiplier=0.5)
    too_small = _create_user(db, enabled=True, limit=1, multiplier=0.5)
    manager = IdleFreeSmallDailyLimitManager(db)
    now = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)

    assert manager.try_reserve(disabled_multiplier, now=now) is None
    assert manager.try_reserve(disabled_daily, now=now) is None
    assert manager.try_reserve(too_small, now=now) is None

    # 曾经启用过、之后把倍率降为 0 时，旧用量仍可查看。
    enabled_user = _create_user(db, enabled=True, limit=5, multiplier=1)
    reservation = manager.try_reserve(enabled_user, now=now)
    manager.confirm(reservation)
    db.execute("UPDATE users SET idle_free_small_multiplier = 0 WHERE id = ?", (enabled_user,))
    snapshot = manager.get_snapshot(enabled_user, now=now)
    assert snapshot.enabled is False
    assert snapshot.used == 1
    assert snapshot.reserved == 0
    db.close()


def test_idle_limit_concurrent_reservation_never_exceeds_limit(tmp_path: Path):
    db = _db(tmp_path)
    user_id = _create_user(db, enabled=True, limit=3, multiplier=1)
    manager = IdleFreeSmallDailyLimitManager(db)
    now = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)

    def try_once(_index: int):
        return manager.try_reserve(user_id, now=now)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(try_once, range(16)))
    assert sum(item is not None for item in results) == 3
    snapshot = manager.get_snapshot(user_id, now=now)
    assert snapshot.used == 0
    assert snapshot.reserved == 3
    db.close()


def test_idle_limit_users_are_independent(tmp_path: Path):
    db = _db(tmp_path)
    manager = IdleFreeSmallDailyLimitManager(db)
    now = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    for _ in range(2):
        user_id = _create_user(db, enabled=True, limit=2, multiplier=1)
        assert manager.try_reserve(user_id, now=now) is not None
        assert manager.try_reserve(user_id, now=now) is not None
        assert manager.try_reserve(user_id, now=now) is None
    db.close()


def test_idle_limit_cross_day_reservation_settles_into_old_window(tmp_path: Path):
    db = _db(tmp_path)
    user_id = _create_user(db, enabled=True, limit=5, multiplier=1)
    manager = IdleFreeSmallDailyLimitManager(db)
    day1 = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    day2 = datetime(2026, 1, 2, 1, tzinfo=timezone.utc)

    reservation = manager.try_reserve(user_id, now=day1)
    manager.confirm(reservation)
    old_snapshot = manager.get_snapshot(user_id, now=day1)
    new_snapshot = manager.get_snapshot(user_id, now=day2)
    assert old_snapshot.used == 1
    assert new_snapshot.used == 0
    assert manager.try_reserve(user_id, now=day2) is not None
    db.close()


def test_idle_limit_batch_snapshots_and_orphan_reclaim(tmp_path: Path):
    db = _db(tmp_path)
    user_id = _create_user(db, enabled=True, limit=5, multiplier=1)
    other_id = _create_user(db, enabled=True, limit=5, multiplier=1)
    manager = IdleFreeSmallDailyLimitManager(db)
    now = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    first = manager.try_reserve(user_id, now=now)
    manager.confirm(first)
    manager.try_reserve(user_id, now=now)
    manager.try_reserve(other_id, now=now)

    snapshots = manager.get_snapshots([user_id, other_id], now=now)
    assert snapshots[user_id].used == 1
    assert snapshots[user_id].reserved == 1
    assert snapshots[other_id].reserved == 1
    assert manager.reclaim_orphan_reserved() == 2
    assert manager.get_snapshot(user_id, now=now).reserved == 0
    assert manager.get_snapshot(other_id, now=now).reserved == 0
    db.close()


def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "idle-daily.db"))
    db.init_schema()
    return db


def _create_user(db: Database, *, enabled: bool, limit: int, multiplier: float) -> int:
    cursor = db.execute(
        """
        INSERT INTO users (
            api_key_hash, name, is_active, free_small_daily_limit_enabled,
            free_small_daily_limit, idle_free_small_multiplier, created_at
        )
        VALUES (?, ?, 1, ?, ?, ?, ?)
        """,
        (f"hash-{uuid.uuid4().hex}", "idle-user", 1 if enabled else 0, limit, multiplier, utc_now_iso()),
    )
    return int(cursor.lastrowid)
