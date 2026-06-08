from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.database import Database, utc_now_iso
from app.free_small_daily_limit import FreeSmallDailyLimitExceeded, FreeSmallDailyLimitManager


def test_free_small_daily_limit_reserves_confirms_and_counts_samples(tmp_path: Path):
    db = _daily_limit_db(tmp_path)
    user_id = _create_user(db, enabled=True, limit=3)
    manager = FreeSmallDailyLimitManager(db)

    reservation = manager.reserve(user_id, 2, now=datetime(2026, 1, 1, 1, tzinfo=timezone.utc))
    snapshot = manager.get_snapshot(user_id, now=datetime(2026, 1, 1, 1, tzinfo=timezone.utc))
    assert snapshot.reserved == 2
    assert snapshot.used == 0
    assert snapshot.available == 1

    manager.confirm(reservation)
    snapshot = manager.get_snapshot(user_id, now=datetime(2026, 1, 1, 1, tzinfo=timezone.utc))
    assert snapshot.used == 2
    assert snapshot.reserved == 0

    with pytest.raises(FreeSmallDailyLimitExceeded) as exceeded:
        manager.reserve(user_id, 2, now=datetime(2026, 1, 1, 1, tzinfo=timezone.utc))
    assert exceeded.value.remaining == 1
    assert exceeded.value.requested == 2


def test_group_limit_is_per_user_and_stricter_limit_wins(tmp_path: Path):
    db = _daily_limit_db(tmp_path)
    group_id = _create_group(db, enabled=True, limit=1)
    first_user = _create_user(db, enabled=True, limit=2, group_id=group_id)
    second_user = _create_user(db, group_id=group_id)
    manager = FreeSmallDailyLimitManager(db)
    now = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)

    first_reservation = manager.reserve(first_user, 1, now=now)
    second_reservation = manager.reserve(second_user, 1, now=now)
    assert first_reservation.scope == "group"
    assert second_reservation.scope == "group"

    manager.confirm(first_reservation)
    manager.confirm(second_reservation)
    assert manager.get_snapshot(first_user, now=now).used == 1
    assert manager.get_snapshot(second_user, now=now).used == 1

    with pytest.raises(FreeSmallDailyLimitExceeded):
        manager.reserve(first_user, 1, now=now)


def test_custom_utc8_reset_hour_changes_window_and_retry_after(tmp_path: Path):
    db = _daily_limit_db(tmp_path)
    user_id = _create_user(db, enabled=True, limit=1)
    manager = FreeSmallDailyLimitManager(db, reset_hour_utc8=6)

    before_reset = datetime(2026, 1, 1, 21, 30, tzinfo=timezone.utc)
    after_reset = datetime(2026, 1, 1, 22, 30, tzinfo=timezone.utc)

    before_snapshot = manager.get_snapshot(user_id, now=before_reset)
    after_snapshot = manager.get_snapshot(user_id, now=after_reset)
    assert before_snapshot.window_start == "2026-01-01T06:00:00+08:00"
    assert before_snapshot.reset_at == "2026-01-02T06:00:00+08:00"
    assert after_snapshot.window_start == "2026-01-02T06:00:00+08:00"

    with pytest.raises(FreeSmallDailyLimitExceeded) as exceeded:
        manager.reserve(user_id, 2, now=before_reset)
    assert exceeded.value.retry_after == 1800


def _daily_limit_db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "daily.db"))
    db.init_schema()
    return db


def _create_user(
    db: Database,
    *,
    enabled: bool = False,
    limit: int = 0,
    group_id: int | None = None,
) -> int:
    cursor = db.execute(
        """
        INSERT INTO users (
            api_key_hash, name, is_active, free_small_daily_limit_enabled,
            free_small_daily_limit, group_id, created_at
        )
        VALUES (?, ?, 1, ?, ?, ?, ?)
        """,
        (
            f"hash-{uuid.uuid4().hex}",
            "daily-user",
            1 if enabled else 0,
            limit,
            group_id,
            utc_now_iso(),
        ),
    )
    return int(cursor.lastrowid)


def _create_group(db: Database, *, enabled: bool, limit: int) -> int:
    cursor = db.execute(
        """
        INSERT INTO user_groups (
            name, is_active, free_small_daily_limit_enabled, free_small_daily_limit, created_at
        )
        VALUES (?, 1, ?, ?, ?)
        """,
        ("daily-group", 1 if enabled else 0, limit, utc_now_iso()),
    )
    return int(cursor.lastrowid)
