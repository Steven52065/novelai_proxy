from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.database import Database, utc_now_iso
from app.quota_manager import InsufficientQuota, QuotaManager, _next_reset_at, normalize_reset_day


def _quota_manager(tmp_path):
    db = Database(str(tmp_path / "quota.db"))
    db.init_schema()
    db.execute(
        """
        INSERT INTO users (api_key_hash, api_key, name, tier, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("hash", "key", "quota-user", "normal", utc_now_iso()),
    )
    manager = QuotaManager(db)
    return db, manager, 1


def test_quota_reserve_confirm_and_release_update_snapshot(tmp_path):
    db, manager, user_id = _quota_manager(tmp_path)
    try:
        manager.create_or_update(user_id, total=10)

        reserved = manager.reserve(user_id, 4)
        assert reserved.available == 6
        assert reserved.reserved == 4

        manager.confirm(user_id, 4)
        after_confirm = manager.get_snapshot(user_id)
        assert after_confirm.used == 4
        assert after_confirm.reserved == 0
        assert after_confirm.available == 6

        manager.reserve(user_id, 3)
        manager.release(user_id, 3)
        after_release = manager.get_snapshot(user_id)
        assert after_release.used == 4
        assert after_release.reserved == 0
        assert after_release.available == 6
    finally:
        db.close()


def test_quota_rejects_when_available_anlas_is_insufficient(tmp_path):
    db, manager, user_id = _quota_manager(tmp_path)
    try:
        manager.create_or_update(user_id, total=2)

        with pytest.raises(InsufficientQuota) as exc_info:
            manager.reserve(user_id, 3)

        assert exc_info.value.need == 3
        assert exc_info.value.have == 2
        assert manager.get_snapshot(user_id).reserved == 0
    finally:
        db.close()


def test_quota_reset_if_due_clears_used_and_reserved(tmp_path):
    db, manager, user_id = _quota_manager(tmp_path)
    try:
        manager.create_or_update(user_id, total=10, reset_period="day")
        manager.reserve(user_id, 5)
        manager.confirm(user_id, 5)
        manager.reserve(user_id, 2)
        old_reset_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        db.execute(
            "UPDATE user_anlas_quota SET last_reset_at = ? WHERE user_id = ?",
            (old_reset_at, user_id),
        )

        assert manager.reset_if_due(user_id) is True

        snapshot = manager.get_snapshot(user_id)
        assert snapshot.used == 0
        assert snapshot.reserved == 0
        assert snapshot.available == 10
    finally:
        db.close()


def test_quota_extra_reset_without_schedule_update_keeps_next_auto_reset(tmp_path):
    db, manager, user_id = _quota_manager(tmp_path)
    try:
        manager.create_or_update(user_id, total=10, reset_period="day")
        manager.reserve(user_id, 5)
        manager.confirm(user_id, 5)
        overdue_reset_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        db.execute(
            "UPDATE user_anlas_quota SET last_reset_at = ? WHERE user_id = ?",
            (overdue_reset_at, user_id),
        )

        manager.reset_usage(user_id, update_last_reset_at=False)

        row = db.query_one(
            "SELECT used, reserved, last_reset_at FROM user_anlas_quota WHERE user_id = ?",
            (user_id,),
        )
        assert row["used"] == 0
        assert row["reserved"] == 0
        assert row["last_reset_at"] == overdue_reset_at
        # 周期未被推迟：原本已到期的自动重置依然会触发。
        assert manager.reset_if_due(user_id) is True
    finally:
        db.close()


def test_reclaim_orphan_reserved_clears_only_reserved_anlas(tmp_path):
    db, manager, user_id = _quota_manager(tmp_path)
    try:
        manager.create_or_update(user_id, total=10, reset_period="never")
        manager.reserve(user_id, 4)
        manager.confirm(user_id, 4)
        manager.reserve(user_id, 3)

        assert manager.reclaim_orphan_reserved() == 1

        snapshot = manager.get_snapshot(user_id)
        assert snapshot.total == 10
        assert snapshot.used == 4
        assert snapshot.reserved == 0
        assert snapshot.available == 6
        assert manager.reclaim_orphan_reserved() == 0
    finally:
        db.close()


def test_monthly_reset_boundary_uses_utc8_midnight():
    last_reset_at = datetime(2026, 6, 30, 16, 0, tzinfo=timezone.utc)

    assert _next_reset_at(last_reset_at, "month", 1) == datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc)


def test_weekly_reset_boundary_uses_utc8_midnight():
    last_reset_at = datetime(2026, 6, 21, 16, 0, tzinfo=timezone.utc)

    assert _next_reset_at(last_reset_at, "week", 1) == datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc)


def test_default_reset_day_uses_utc8_local_date():
    utc_time_at_utc8_month_start = datetime(2026, 6, 30, 16, 0, tzinfo=timezone.utc)
    utc_time_at_utc8_monday = datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc)

    assert normalize_reset_day("month", None, utc_time_at_utc8_month_start) == 1
    assert normalize_reset_day("week", None, utc_time_at_utc8_monday) == 1
