from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

from app.database import Database
from app.database import connection as db_connection
from app.database_maintenance import next_daily_run_utc8
from app.timezones import DISPLAY_TIMEZONE


def test_next_daily_run_utc8_uses_same_day_before_target_time():
    now = datetime(2026, 6, 22, 3, 59, tzinfo=DISPLAY_TIMEZONE)
    next_run = next_daily_run_utc8(now.astimezone(timezone.utc), "04:00")

    assert next_run.astimezone(DISPLAY_TIMEZONE) == datetime(2026, 6, 22, 4, 0, tzinfo=DISPLAY_TIMEZONE)


def test_next_daily_run_utc8_uses_next_day_at_or_after_target_time():
    now = datetime(2026, 6, 22, 4, 0, tzinfo=DISPLAY_TIMEZONE)
    next_run = next_daily_run_utc8(now.astimezone(timezone.utc), "04:00")

    assert next_run.astimezone(DISPLAY_TIMEZONE) == datetime(2026, 6, 23, 4, 0, tzinfo=DISPLAY_TIMEZONE)


def test_vacuum_uses_independent_connection_without_blocking_main_lock(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "vacuum.db"))
    db.init_schema()
    db.execute(
        """
        INSERT INTO users (api_key_hash, name, created_at)
        VALUES (?, ?, ?)
        """,
        ("hash-before-vacuum", "before-vacuum", datetime.now(timezone.utc).isoformat()),
    )

    original_connect = sqlite3.connect
    vacuum_started = threading.Event()
    release_vacuum = threading.Event()
    errors: list[BaseException] = []

    class PausingConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().upper() == "VACUUM":
                vacuum_started.set()
                assert release_vacuum.wait(timeout=5)
            return self._conn.execute(sql, *args, **kwargs)

        def close(self):
            return self._conn.close()

    def connect_for_vacuum(*args, **kwargs):
        return PausingConnection(original_connect(*args, **kwargs))

    monkeypatch.setattr(db_connection.sqlite3, "connect", connect_for_vacuum)

    def run_vacuum():
        try:
            db.vacuum()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_vacuum)
    thread.start()
    try:
        assert vacuum_started.wait(timeout=5)
        db.execute(
            """
            INSERT INTO users (api_key_hash, name, created_at)
            VALUES (?, ?, ?)
            """,
            ("hash-during-vacuum", "during-vacuum", datetime.now(timezone.utc).isoformat()),
        )
        row = db.query_one("SELECT COUNT(*) AS count FROM users")
        assert int(row["count"]) == 2
    finally:
        release_vacuum.set()
        thread.join(timeout=5)
        db.close()

    assert not thread.is_alive()
    assert errors == []
