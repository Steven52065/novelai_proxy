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


def test_runtime_limit_uses_only_user_level_config(tmp_path: Path):
    db = _daily_limit_db(tmp_path)
    group_id = _create_group(db, enabled=True, limit=1)
    configured_user = _create_user(db, enabled=True, limit=2, group_id=group_id)
    unconfigured_user = _create_user(db, group_id=group_id)
    manager = FreeSmallDailyLimitManager(db)
    now = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)

    # 组配置不再在运行时合并：用户自身限制为 2，可以连续使用 2 张。
    first_reservation = manager.reserve(configured_user, 1, now=now)
    assert first_reservation.scope == "user"
    assert first_reservation.limit == 2
    manager.confirm(first_reservation)
    second_reservation = manager.reserve(configured_user, 1, now=now)
    manager.confirm(second_reservation)
    with pytest.raises(FreeSmallDailyLimitExceeded):
        manager.reserve(configured_user, 1, now=now)

    # 用户级未启用限制时，组配置也不会限制该用户。
    assert manager.reserve(unconfigured_user, 1, now=now) is None
    assert manager.get_snapshot(unconfigured_user, now=now).enabled is False


def test_init_schema_migrates_group_daily_limit_into_members(tmp_path: Path):
    db_path = str(tmp_path / "migrate.db")
    db = Database(db_path)
    db.init_schema()
    active_group = _create_group(db, enabled=True, limit=5)
    inactive_group = _create_group(db, enabled=True, limit=9)
    db.execute("UPDATE user_groups SET is_active = 0 WHERE id = ?", (inactive_group,))
    follower = _create_user(db, group_id=active_group)
    custom_user = _create_user(db, enabled=True, limit=2, group_id=active_group)
    inactive_member = _create_user(db, group_id=inactive_group)
    # 模拟旧版本数据库：迁移标记复位后重新初始化。
    db.execute("PRAGMA user_version = 0")
    db.close()

    reopened = Database(db_path)
    reopened.init_schema()
    rows = {
        int(row["id"]): (int(row["free_small_daily_limit_enabled"]), int(row["free_small_daily_limit"]))
        for row in reopened.query_all("SELECT id, free_small_daily_limit_enabled, free_small_daily_limit FROM users")
    }
    assert rows[follower] == (1, 5)
    assert rows[custom_user] == (1, 2)
    assert rows[inactive_member] == (0, 0)

    # 再次初始化不会重复迁移：管理员手动关闭后保持关闭。
    reopened.execute(
        "UPDATE users SET free_small_daily_limit_enabled = 0, free_small_daily_limit = 0 WHERE id = ?",
        (follower,),
    )
    reopened.init_schema()
    row = reopened.query_one(
        "SELECT free_small_daily_limit_enabled, free_small_daily_limit FROM users WHERE id = ?",
        (follower,),
    )
    assert (int(row["free_small_daily_limit_enabled"]), int(row["free_small_daily_limit"])) == (0, 0)
    reopened.close()


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
