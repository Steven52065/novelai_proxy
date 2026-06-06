from __future__ import annotations

import threading
import uuid

from app.database import Database, utc_now_iso
from app.quota_manager import InsufficientQuota, QuotaManager


def _create_user_with_quota(db: Database, quota_manager: QuotaManager, *, total: int) -> int:
    cursor = db.execute(
        """
        INSERT INTO users (api_key_hash, name, tier, is_active, free_small_only, allowed_endpoints, created_at)
        VALUES (?, ?, 'normal', 1, 0, 'generate-image', ?)
        """,
        (f"hash-{uuid.uuid4().hex}", "quota-concurrent-user", utc_now_iso()),
    )
    user_id = int(cursor.lastrowid)
    quota_manager.create_or_update(user_id, total)
    return user_id


def test_concurrent_quota_reservations_do_not_exceed_available_anlas(tmp_path):
    db = Database(str(tmp_path / "quota.db"))
    db.init_schema()
    quota_manager = QuotaManager(db)
    user_id = _create_user_with_quota(db, quota_manager, total=10)
    start = threading.Barrier(4)
    results = []
    lock = threading.Lock()

    def reserve_four():
        start.wait(timeout=3)
        try:
            quota_manager.reserve(user_id, 4)
        except InsufficientQuota as exc:
            result = ("rejected", exc.need, exc.have)
        else:
            result = ("reserved",)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=reserve_four) for _ in range(4)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
    finally:
        db.close()

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(result[0] for result in results) == ["rejected", "rejected", "reserved", "reserved"]
    assert QuotaManager(Database(str(tmp_path / "quota.db"))).get_snapshot(user_id).reserved == 8


def test_concurrent_quota_reservations_are_isolated_per_user(tmp_path):
    db = Database(str(tmp_path / "quota.db"))
    db.init_schema()
    quota_manager = QuotaManager(db)
    first_user = _create_user_with_quota(db, quota_manager, total=5)
    second_user = _create_user_with_quota(db, quota_manager, total=5)
    start = threading.Barrier(2)

    def reserve(user_id: int):
        start.wait(timeout=3)
        quota_manager.reserve(user_id, 5)

    threads = [threading.Thread(target=reserve, args=(user_id,)) for user_id in (first_user, second_user)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        assert all(not thread.is_alive() for thread in threads)
        assert quota_manager.get_snapshot(first_user).reserved == 5
        assert quota_manager.get_snapshot(second_user).reserved == 5
    finally:
        db.close()
