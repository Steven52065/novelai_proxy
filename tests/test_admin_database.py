from __future__ import annotations

import sqlite3
import inspect
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.database import Database
from app.database import utc_now_iso, validate_discord_self_service_config
from app.payload_archive import PayloadArchiveService
from app.usage_logs import UsageLogCreate
from helpers import csrf_form, write_test_config


def _utf8_len(value: str | None) -> int:
    return len(value.encode("utf-8")) if value is not None else 0


def _insert_precomputed_usage_log(
    db: Database,
    *,
    request_id: str,
    user_id: int,
    created_at: str,
    request_payload: str | None = None,
    output_files: str | None = None,
    image_urls: str | None = None,
    status: str = "success",
) -> None:
    request_payload_bytes = _utf8_len(request_payload)
    db.execute(
        """
        INSERT INTO usage_logs (
            request_id, user_id, action, estimated_anlas_cost, status, log_level,
            request_payload, request_payload_bytes, request_payload_available_bytes,
            output_files, output_files_bytes, image_urls, image_urls_bytes, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            user_id,
            "generate",
            0,
            status,
            "INFO",
            request_payload,
            request_payload_bytes,
            request_payload_bytes,
            output_files,
            _utf8_len(output_files),
            image_urls,
            _utf8_len(image_urls),
            created_at,
        ),
    )


def test_admin_database_management_clears_large_payloads(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    old_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    recent_time = datetime.now(timezone.utc).isoformat()
    large_payload = '{"image":"' + ("a" * 2048) + '"}'

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "db-clean-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        for request_id, created_at in (("old-large-payload", old_time), ("recent-large-payload", recent_time)):
            _insert_precomputed_usage_log(
                app.state.db,
                request_id=request_id,
                user_id=user_id,
                request_payload=large_payload,
                output_files='["image.png"]',
                image_urls='[{"url":"https://files.catbox.moe/image.png"}]',
                created_at=created_at,
            )
        _insert_precomputed_usage_log(
            app.state.db,
            request_id="old-large-image-urls",
            user_id=user_id,
            request_payload=None,
            output_files="[]",
            image_urls='[{"url":"https://files.catbox.moe/' + ("b" * 2048) + '.png"}]',
            created_at=old_time,
        )

        stats = client.get("/admin/api/database/stats", auth=("admin", "admin123"))
        assert stats.status_code == 200
        assert stats.json()["usage_logs"]["logs_with_payload"] == 2

        clear_resp = client.post(
            "/admin/api/database/clear-payloads",
            auth=("admin", "admin123"),
            json={"older_than_days": 7, "min_payload_kb": 1, "clear_output_files": False},
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json()["updated_logs"] == 1

        old_row = app.state.db.query_one(
            "SELECT request_payload, output_files, image_urls FROM usage_logs WHERE request_id = ?",
            ("old-large-payload",),
        )
        recent_row = app.state.db.query_one(
            "SELECT request_payload, output_files, image_urls FROM usage_logs WHERE request_id = ?",
            ("recent-large-payload",),
        )
        assert old_row["request_payload"] is None
        assert old_row["output_files"] == '["image.png"]'
        assert old_row["image_urls"] == '[{"url":"https://files.catbox.moe/image.png"}]'
        assert recent_row["request_payload"] == large_payload

        clear_urls_resp = client.post(
            "/admin/api/database/clear-payloads",
            auth=("admin", "admin123"),
            json={"older_than_days": 7, "min_payload_kb": 1, "clear_image_urls": True},
        )
        assert clear_urls_resp.status_code == 200
        assert clear_urls_resp.json()["updated_logs"] == 1

        image_url_row = app.state.db.query_one(
            "SELECT request_payload, output_files, image_urls FROM usage_logs WHERE request_id = ?",
            ("old-large-image-urls",),
        )
        assert image_url_row["request_payload"] is None
        assert image_url_row["output_files"] == "[]"
        assert image_url_row["image_urls"] is None


def test_admin_database_clear_payloads_purges_archived_payloads(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    old_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    large_payload = '{"input":"' + ("a" * 2048) + '","parameters":{"steps":1}}'

    with TestClient(app) as client:
        user_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "db-clear-archived", "tier": "normal", "anlas_total": 100},
        ).json()["user_id"]
        _insert_precomputed_usage_log(
            app.state.db,
            request_id="clear-archived-payload",
            user_id=user_id,
            request_payload=large_payload,
            created_at=old_time,
        )

        archive_result = app.state.payload_archive_service.archive_due_payloads(
            now=datetime.now(timezone.utc),
            hot_days=7,
        )
        assert archive_result["archived_payloads"] == 1
        source = app.state.db.query_one(
            "SELECT id, request_payload FROM usage_logs WHERE request_id = ?",
            ("clear-archived-payload",),
        )
        assert source["request_payload"] is None
        before_payload = client.get(
            f"/admin/api/logs/by-id/{source['id']}/payload",
            auth=("admin", "admin123"),
        )
        assert before_payload.status_code == 200

        clear_resp = client.post(
            "/admin/api/database/clear-payloads",
            auth=("admin", "admin123"),
            json={"older_than_days": 7, "min_payload_kb": 1},
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json()["updated_logs"] == 1
        assert clear_resp.json()["purged_archived_payloads"] == 1
        assert clear_resp.json()["deleted_archives"] == 1

        assert app.state.db.query_one("SELECT COUNT(*) AS count FROM usage_log_payload_archive_refs")["count"] == 0
        assert app.state.db.query_one("SELECT COUNT(*) AS count FROM usage_log_payload_archives")["count"] == 0
        after_payload = client.get(
            f"/admin/api/logs/by-id/{source['id']}/payload",
            auth=("admin", "admin123"),
        )
        assert after_payload.status_code == 404
        log = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"][0]
        assert log["has_request_payload"] is False
        assert log["payload_archived"] is False
        assert log["request_payload_bytes"] == 0


def test_admin_database_stats_and_clear_payloads_handle_hot_compressed_payloads(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "NOVELAI_PROXY_CONFIG",
        str(write_test_config(tmp_path, hot_payload_enabled=True, hot_payload_min_bytes=100)),
    )
    from app.main import app

    old_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    large_payload = {
        "input": "hot database cleanup " * 600,
        "model": "nai-diffusion-3",
        "parameters": {"width": 512, "height": 768, "steps": 1, "n_samples": 1},
    }

    with TestClient(app) as client:
        user_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "db-hot-payload-clear", "tier": "normal", "anlas_total": 100},
        ).json()["user_id"]
        app.state.usage_logs.insert_queued(
            UsageLogCreate(
                request_id="old-hot-compressed-payload",
                user_id=user_id,
                action="generate",
                estimated_anlas_cost=0,
                request_payload=large_payload,
            )
        )
        app.state.db.execute(
            "UPDATE usage_logs SET created_at = ? WHERE request_id = ?",
            (old_time, "old-hot-compressed-payload"),
        )
        source = app.state.db.query_one(
            "SELECT * FROM usage_logs WHERE request_id = ?",
            ("old-hot-compressed-payload",),
        )
        assert source["request_payload_encoding"] == "zlib"

        stats = client.get("/admin/api/database/stats", auth=("admin", "admin123")).json()
        assert stats["usage_logs"]["logs_with_payload"] == 1
        assert stats["usage_logs"]["request_payload_bytes"] == source["request_payload_bytes"]
        assert stats["usage_logs"]["request_payload_compressed_bytes"] == source["request_payload_compressed_bytes"]
        assert stats["largest_logs"][0]["request_payload_bytes"] == source["request_payload_bytes"]

        clear_resp = client.post(
            "/admin/api/database/clear-payloads",
            auth=("admin", "admin123"),
            json={"older_than_days": 7, "min_payload_kb": 1},
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json()["updated_logs"] == 1

        cleared = app.state.db.query_one(
            "SELECT * FROM usage_logs WHERE request_id = ?",
            ("old-hot-compressed-payload",),
        )
        assert cleared["request_payload"] is None
        assert cleared["request_payload_encoding"] == "json"
        assert cleared["request_payload_blob"] is None
        assert cleared["request_payload_bytes"] == 0
        assert cleared["request_payload_compressed_bytes"] == 0
        after_payload = client.get(
            f"/admin/api/logs/by-id/{cleared['id']}/payload",
            auth=("admin", "admin123"),
        )
        assert after_payload.status_code == 404


def test_admin_database_clear_payloads_counts_legacy_text_payload_bytes(tmp_path: Path, monkeypatch):
    config_path = write_test_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))

    old_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    legacy_payload = '{"input":"' + ("中文提示词" * 80) + '","parameters":{"steps":1}}'
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key_hash TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            estimated_anlas_cost INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            log_level TEXT NOT NULL DEFAULT 'INFO',
            request_payload TEXT,
            request_payload_bytes INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        INSERT INTO users(api_key_hash, name, created_at)
        VALUES ('legacy-hash', 'legacy-user', '2026-01-01T00:00:00+00:00');
        """
    )
    conn.execute(
        """
        INSERT INTO usage_logs (
            request_id, user_id, action, estimated_anlas_cost, status, log_level,
            request_payload, request_payload_bytes, created_at
        )
        VALUES (?, 1, 'generate', 0, 'success', 'INFO', ?, 0, ?)
        """,
        ("old-legacy-nonascii-payload", legacy_payload, old_time),
    )
    conn.commit()
    conn.close()

    from app.main import app

    with TestClient(app) as client:
        expected_bytes = len(legacy_payload.encode("utf-8"))
        assert expected_bytes >= 1024
        assert len(legacy_payload) < 1024

        stats = client.get("/admin/api/database/stats", auth=("admin", "admin123"))
        assert stats.status_code == 200
        assert stats.json()["usage_logs"]["request_payload_bytes"] == expected_bytes

        log = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"][0]
        assert log["request_payload_bytes"] == expected_bytes

        clear_resp = client.post(
            "/admin/api/database/clear-payloads",
            auth=("admin", "admin123"),
            json={"older_than_days": 7, "min_payload_kb": 1},
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json()["updated_logs"] == 1
        cleared = app.state.db.query_one(
            "SELECT request_payload FROM usage_logs WHERE request_id = ?",
            ("old-legacy-nonascii-payload",),
        )
        assert cleared["request_payload"] is None


def test_usage_log_size_fields_migration_backfills_archived_and_json_sizes(tmp_path: Path):
    db_path = tmp_path / "old-size-fields.db"
    payload = '{"input":"legacy hot","parameters":{"steps":1}}'
    output_files = '["legacy.png"]'
    image_urls = '[{"url":"https://files.example/legacy.png"}]'
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        PRAGMA user_version = 1;
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key_hash TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL DEFAULT 0,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            estimated_anlas_cost INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            log_level TEXT NOT NULL DEFAULT 'INFO',
            request_payload TEXT,
            request_payload_encoding TEXT NOT NULL DEFAULT 'json',
            request_payload_blob BLOB,
            request_payload_bytes INTEGER NOT NULL DEFAULT 0,
            request_payload_compressed_bytes INTEGER NOT NULL DEFAULT 0,
            output_files TEXT,
            image_urls TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(request_id, attempt_number)
        );
        CREATE TABLE usage_log_payload_archives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            archive_date TEXT NOT NULL,
            part_number INTEGER NOT NULL,
            compression TEXT NOT NULL,
            compression_level INTEGER NOT NULL,
            payload_count INTEGER NOT NULL,
            raw_bytes INTEGER NOT NULL,
            compressed_bytes INTEGER NOT NULL,
            payload_sha256 TEXT NOT NULL,
            payload_blob BLOB NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(archive_date, part_number)
        );
        CREATE TABLE usage_log_payload_archive_refs (
            log_id INTEGER PRIMARY KEY,
            archive_id INTEGER NOT NULL,
            payload_key TEXT NOT NULL,
            payload_bytes INTEGER NOT NULL
        );
        INSERT INTO users(api_key_hash, name, created_at)
        VALUES ('hash', 'user', '2026-01-01T00:00:00+00:00');
        INSERT INTO usage_log_payload_archives (
            id, archive_date, part_number, compression, compression_level, payload_count,
            raw_bytes, compressed_bytes, payload_sha256, payload_blob, created_at
        )
        VALUES (1, '2026-01-01', 1, 'zlib', 6, 1, 10, 10, 'sha', X'00', '2026-01-01T00:00:00+00:00');
        INSERT INTO usage_logs (
            id, request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at
        )
        VALUES (1, 'archived-legacy', 1, 'generate', 0, 'success', 'INFO', '2026-01-01T00:00:00+00:00');
        INSERT INTO usage_log_payload_archive_refs(log_id, archive_id, payload_key, payload_bytes)
        VALUES (1, 1, '1', 4321);
        """
    )
    conn.execute(
        """
        INSERT INTO usage_logs (
            id, request_id, user_id, action, estimated_anlas_cost, status, log_level,
            request_payload, request_payload_bytes, output_files, image_urls, created_at
        )
        VALUES (2, 'hot-legacy', 1, 'generate', 0, 'success', 'INFO', ?, 0, ?, ?, '2026-01-01T00:00:01+00:00')
        """,
        (payload, output_files, image_urls),
    )
    conn.commit()
    conn.close()

    db = Database(str(db_path))
    db.init_schema()

    archived = db.query_one("SELECT * FROM usage_logs WHERE request_id = ?", ("archived-legacy",))
    hot = db.query_one("SELECT * FROM usage_logs WHERE request_id = ?", ("hot-legacy",))
    assert archived["request_payload_available_bytes"] == 4321
    assert archived["request_payload_bytes"] == 0
    assert hot["request_payload_bytes"] == len(payload.encode("utf-8"))
    assert hot["request_payload_available_bytes"] == len(payload.encode("utf-8"))
    assert hot["output_files_bytes"] == len(output_files.encode("utf-8"))
    assert hot["image_urls_bytes"] == len(image_urls.encode("utf-8"))
    assert db.query_one("PRAGMA user_version")[0] == 2
    db.close()


def test_database_size_report_uses_precomputed_fields_and_index():
    from app.admin import database as admin_database

    db = Database(":memory:")
    db.init_schema()
    db.execute(
        "INSERT INTO users(api_key_hash, name, created_at) VALUES (?, ?, ?)",
        ("hash", "user", "2026-01-01T00:00:00+00:00"),
    )
    db.execute(
        """
        INSERT INTO usage_logs (
            request_id, user_id, action, estimated_anlas_cost, status, log_level,
            request_payload_available_bytes, output_files_bytes, image_urls_bytes, created_at
        )
        VALUES ('size-plan', 1, 'generate', 0, 'success', 'INFO', 3, 2, 1, '2026-01-01T00:00:00+00:00')
        """
    )

    plan_rows = db.query_all(
        """
        EXPLAIN QUERY PLAN
        SELECT l.id, l.request_id, u.name AS user_name
        FROM usage_logs l
        JOIN users u ON u.id = l.user_id
        ORDER BY l.request_payload_available_bytes DESC, l.output_files_bytes DESC, l.image_urls_bytes DESC, l.id DESC
        LIMIT 10
        """
    )
    plan_text = "\n".join(str(row["detail"]) for row in plan_rows)
    assert "idx_usage_size_report" in plan_text

    stats_source = inspect.getsource(admin_database._database_stats)
    assert "LENGTH(l.output_files)" not in stats_source
    assert "LENGTH(l.image_urls)" not in stats_source
    assert "LENGTH(l.request_payload_blob)" not in stats_source
    db.close()


def test_admin_database_management_deletes_old_logs_by_status(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    old_time = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    recent_time = datetime.now(timezone.utc).isoformat()

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "db-delete-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        rows = [
            ("old-success-log", "success", old_time),
            ("old-rejected-log", "rejected", old_time),
            ("recent-rejected-log", "rejected", recent_time),
        ]
        for request_id, status, created_at in rows:
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, user_id, "generate", 0, status, "INFO", created_at),
            )

        cleanup_resp = client.post(
            "/admin/api/database/cleanup-logs",
            auth=("admin", "admin123"),
            json={"older_than_days": 30, "statuses": ["rejected"]},
        )
        assert cleanup_resp.status_code == 200
        assert cleanup_resp.json()["deleted_logs"] == 1

        remaining_ids = {
            row["request_id"]
            for row in app.state.db.query_all("SELECT request_id FROM usage_logs ORDER BY request_id")
        }
        assert "old-rejected-log" not in remaining_ids
        assert {"old-success-log", "recent-rejected-log"} <= remaining_ids


def test_admin_database_cleanup_deletes_logs_in_batches_and_updates_dashboard_stats(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    old_time = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()

    with TestClient(app) as client:
        user_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "db-batch-delete-user", "tier": "normal", "anlas_total": 100},
        ).json()["user_id"]
        with app.state.db.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (f"old-batch-log-{index}", user_id, "generate", 0, "failed", "INFO", old_time)
                    for index in range(1200)
                ],
            )

        before_stats = app.state.db.query_one(
            "SELECT COALESCE(SUM(request_count), 0) AS count FROM dashboard_hourly_stats"
        )
        assert int(before_stats["count"]) == 1200

        cleanup_resp = client.post(
            "/admin/api/database/cleanup-logs",
            auth=("admin", "admin123"),
            json={"older_than_days": 30, "statuses": ["failed"]},
        )

        assert cleanup_resp.status_code == 200
        assert cleanup_resp.json()["deleted_logs"] == 1200
        assert app.state.db.query_one("SELECT COUNT(*) AS count FROM usage_logs")["count"] == 0
        after_stats = app.state.db.query_one(
            "SELECT COALESCE(SUM(request_count), 0) AS count FROM dashboard_hourly_stats"
        )
        assert int(after_stats["count"]) == 0


def test_admin_database_cleanup_uses_same_batch_for_archive_scan_and_delete(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    old_time = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    archive_select_sql: list[str] = []

    with TestClient(app) as client:
        user_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "db-cleanup-target-batch", "tier": "normal", "anlas_total": 100},
        ).json()["user_id"]
        _insert_precomputed_usage_log(
            app.state.db,
            request_id="cleanup-target-batch",
            user_id=user_id,
            status="failed",
            request_payload='{"input":"cleanup target batch","parameters":{"steps":1}}',
            created_at=old_time,
        )
        archive_result = app.state.payload_archive_service.archive_due_payloads(
            now=datetime.now(timezone.utc),
            hot_days=7,
        )
        assert archive_result["archived_payloads"] == 1

        original_transaction = app.state.db.transaction

        class RecordingConnection:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, params=()):
                if "SELECT DISTINCT r.archive_id" in sql:
                    archive_select_sql.append(sql)
                return self._conn.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        @contextmanager
        def transaction_with_recording(*args, **kwargs):
            with original_transaction(*args, **kwargs) as conn:
                yield RecordingConnection(conn)

        monkeypatch.setattr(app.state.db, "transaction", transaction_with_recording)

        cleanup_resp = client.post(
            "/admin/api/database/cleanup-logs",
            auth=("admin", "admin123"),
            json={"older_than_days": 30, "statuses": ["failed"]},
        )

        assert cleanup_resp.status_code == 200
        assert cleanup_resp.json()["deleted_logs"] == 1
        assert archive_select_sql
        assert all("temp_log_cleanup_targets" in sql for sql in archive_select_sql)
        assert app.state.db.query_one("SELECT COUNT(*) AS count FROM usage_logs")["count"] == 0


def test_admin_database_cleanup_prevents_archive_insert_between_scan_and_delete(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    old_time = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()

    with TestClient(app) as client:
        user_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "db-cleanup-race", "tier": "normal", "anlas_total": 100},
        ).json()["user_id"]
        _insert_precomputed_usage_log(
            app.state.db,
            request_id="cleanup-archive-race",
            user_id=user_id,
            status="failed",
            request_payload='{"input":"cleanup race","parameters":{"steps":1}}',
            created_at=old_time,
        )

        competing_db = Database(str(app.state.db.path))
        competing_db.conn.execute("PRAGMA busy_timeout = 1")
        competing_archive = PayloadArchiveService(competing_db)
        archive_race_errors: list[str] = []
        original_transaction = app.state.db.transaction

        class ArchiveRaceConnection:
            def __init__(self, conn):
                self._conn = conn
                self._triggered = False

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, params=()):
                cursor = self._conn.execute(sql, params)
                if (
                    not self._triggered
                    and "SELECT DISTINCT r.archive_id" in sql
                    and "usage_log_payload_archive_refs" in sql
                ):
                    self._triggered = True
                    try:
                        competing_archive.archive_due_payloads(
                            now=datetime.now(timezone.utc),
                            hot_days=7,
                        )
                    except sqlite3.OperationalError as exc:
                        archive_race_errors.append(str(exc))
                return cursor

        @contextmanager
        def transaction_with_archive_race(*args, **kwargs):
            with original_transaction(*args, **kwargs) as conn:
                yield ArchiveRaceConnection(conn)

        monkeypatch.setattr(app.state.db, "transaction", transaction_with_archive_race)

        try:
            cleanup_resp = client.post(
                "/admin/api/database/cleanup-logs",
                auth=("admin", "admin123"),
                json={"older_than_days": 30, "statuses": ["failed"]},
            )
            assert cleanup_resp.status_code == 200
            assert any("locked" in error.lower() for error in archive_race_errors)
            assert cleanup_resp.json()["deleted_logs"] == 1
            assert app.state.db.query_one("SELECT COUNT(*) AS count FROM usage_logs")["count"] == 0
            assert app.state.db.query_one("SELECT COUNT(*) AS count FROM usage_log_payload_archive_refs")["count"] == 0
            assert app.state.db.query_one("SELECT COUNT(*) AS count FROM usage_log_payload_archives")["count"] == 0
        finally:
            competing_db.close()


def test_admin_database_archives_old_payloads_and_keeps_recent_hot(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    from app.timezones import DISPLAY_TIMEZONE

    now = datetime.now(timezone.utc)
    cutoff_date = (now.astimezone(DISPLAY_TIMEZONE) - timedelta(days=7)).date()
    cutoff_local = datetime.combine(cutoff_date, datetime.min.time(), tzinfo=DISPLAY_TIMEZONE)
    old_time = (cutoff_local - timedelta(seconds=1)).astimezone(timezone.utc).isoformat()
    boundary_time = cutoff_local.isoformat()
    recent_time = now.isoformat()

    with TestClient(app) as client:
        user_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "db-archive-user", "tier": "normal", "anlas_total": 100},
        ).json()["user_id"]
        for request_id, payload, created_at in (
            ("archive-old", '{"input":"old","parameters":{"steps":1}}', old_time),
            ("archive-boundary", '{"input":"boundary","parameters":{"steps":1}}', boundary_time),
            ("archive-recent", '{"input":"recent","parameters":{"steps":1}}', recent_time),
        ):
            _insert_precomputed_usage_log(
                app.state.db,
                request_id=request_id,
                user_id=user_id,
                request_payload=payload,
                created_at=created_at,
            )

        archive = client.post(
            "/admin/api/database/archive-payloads",
            auth=("admin", "admin123"),
            json={"hot_days": 7, "max_rows": 100},
        )
        assert archive.status_code == 200
        assert archive.json()["archived_payloads"] == 1

        rows = {
            row["request_id"]: row
            for row in app.state.db.query_all(
                "SELECT request_id, request_payload FROM usage_logs WHERE request_id LIKE 'archive-%'"
            )
        }
        assert rows["archive-old"]["request_payload"] is None
        assert rows["archive-boundary"]["request_payload"] == '{"input":"boundary","parameters":{"steps":1}}'
        assert rows["archive-recent"]["request_payload"] == '{"input":"recent","parameters":{"steps":1}}'

        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        archived_log = next(row for row in logs if row["request_id"] == "archive-old")
        assert archived_log["request_payload"] is None
        assert archived_log["has_request_payload"] is True
        assert archived_log["payload_archived"] is True
        assert archived_log["request_payload_bytes"] > 0

        payload = client.get(
            f"/admin/api/logs/by-id/{archived_log['id']}/payload",
            auth=("admin", "admin123"),
        )
        assert payload.status_code == 200
        assert payload.json()["request_payload"]["input"] == "old"

        stats = client.get("/admin/api/database/stats", auth=("admin", "admin123")).json()
        assert stats["payload_archives"]["payload_count"] == 1
        assert stats["payload_archives"]["archive_count"] == 1
        assert stats["payload_archives"]["candidate_count"] == 0


def test_admin_database_archive_splits_by_part_limits(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        user_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "db-archive-parts", "tier": "normal", "anlas_total": 100},
        ).json()["user_id"]
        for index in range(3):
            _insert_precomputed_usage_log(
                app.state.db,
                request_id=f"archive-part-{index}",
                user_id=user_id,
                request_payload=f'{{"index":{index}}}',
                created_at="2026-05-10T00:00:00+00:00",
            )

        result = app.state.payload_archive_service.archive_due_payloads(
            now=datetime(2026, 5, 22, 12, tzinfo=timezone.utc),
            hot_days=7,
            max_payloads_per_part=2,
        )
        assert result["archived_payloads"] == 3
        assert result["archived_parts"] == 2

        parts = app.state.db.query_all(
            "SELECT archive_date, part_number, payload_count FROM usage_log_payload_archives ORDER BY part_number"
        )
        assert [row["payload_count"] for row in parts] == [2, 1]
        assert {row["archive_date"] for row in parts} == {"2026-05-10"}


def test_admin_database_cleanup_compacts_archived_payloads(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        user_id = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "db-archive-compact", "tier": "normal", "anlas_total": 100},
        ).json()["user_id"]
        for request_id, status in (("compact-delete", "failed"), ("compact-keep", "success")):
            _insert_precomputed_usage_log(
                app.state.db,
                request_id=request_id,
                user_id=user_id,
                status=status,
                request_payload=f'{{"request_id":"{request_id}"}}',
                created_at="2026-05-10T00:00:00+00:00",
            )
        app.state.payload_archive_service.archive_due_payloads(
            now=datetime(2026, 5, 22, 12, tzinfo=timezone.utc),
            hot_days=7,
        )
        keep_log = app.state.db.query_one("SELECT id FROM usage_logs WHERE request_id = ?", ("compact-keep",))
        delete_log = app.state.db.query_one("SELECT id FROM usage_logs WHERE request_id = ?", ("compact-delete",))
        archive_id = app.state.db.query_one(
            "SELECT archive_id FROM usage_log_payload_archive_refs WHERE log_id = ?",
            (delete_log["id"],),
        )["archive_id"]

        cleanup = client.post(
            "/admin/api/database/cleanup-logs",
            auth=("admin", "admin123"),
            json={"older_than_days": 7, "statuses": ["failed"]},
        )
        assert cleanup.status_code == 200
        assert cleanup.json()["deleted_logs"] == 1
        archive = app.state.db.query_one(
            "SELECT payload_count FROM usage_log_payload_archives WHERE id = ?",
            (archive_id,),
        )
        assert archive["payload_count"] == 1
        assert app.state.payload_archive_service.get_payload_dict(keep_log["id"])["request_id"] == "compact-keep"
        assert app.state.db.query_one("SELECT id FROM usage_logs WHERE id = ?", (delete_log["id"],)) is None

def test_admin_database_page_and_vacuum(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        page = client.get("/admin/database")
        assert page.status_code == 200
        assert "数据库管理" in page.text
        assert "清空大 Payload" in page.text

        api_resp = client.post("/admin/api/database/vacuum", auth=("admin", "admin123"))
        assert api_resp.status_code == 200
        assert api_resp.json()["ok"] is True

        form_resp = client.post("/admin/database/vacuum", data=csrf_form(client), follow_redirects=False)
        assert form_resp.status_code == 303
        assert "/admin/database" in form_resp.headers["location"]


def test_init_schema_migrates_self_service_tables_and_nullable_user_group(tmp_path: Path):
    db_path = tmp_path / "old-users.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key_hash TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    db = Database(str(db_path))
    db.init_schema()
    tables = {
        row["name"]
        for row in db.query_all("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "user_groups",
        "group_rate_limit_rules",
        "group_member_rate_limit_rules",
        "discord_user_links",
        "usage_log_payload_archives",
        "usage_log_payload_archive_refs",
    } <= tables

    user_columns = {row["name"] for row in db.query_all("PRAGMA table_info(users)")}
    assert "group_id" in user_columns
    assert "image_format_policy" in user_columns
    group_columns = {row["name"] for row in db.query_all("PRAGMA table_info(user_groups)")}
    assert "default_image_format_policy" in group_columns
    db.execute(
        "INSERT INTO users(api_key_hash, name, created_at) VALUES (?, ?, ?)",
        ("legacy-hash", "legacy-user", utc_now_iso()),
    )
    user = db.query_one("SELECT group_id, image_format_policy FROM users WHERE api_key_hash = ?", ("legacy-hash",))
    assert user["group_id"] is None
    assert user["image_format_policy"] == "follow_global"
    db.execute(
        "INSERT INTO user_groups(name, is_active, created_at) VALUES (?, 1, ?)",
        ("legacy-group", utc_now_iso()),
    )
    group = db.query_one("SELECT default_image_format_policy FROM user_groups WHERE name = ?", ("legacy-group",))
    assert group["default_image_format_policy"] == "follow_global"

    indexes = {
        row["name"]
        for row in db.query_all("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert "idx_users_group_id" in indexes
    assert "idx_group_rate_limit_rules_group_id" in indexes
    assert "idx_group_member_rate_limit_rules_group_id" in indexes
    assert "idx_rate_limit_rules_user_id" in indexes
    assert {"idx_usage_created_at", "idx_usage_action_created", "idx_usage_status_created"} <= indexes
    assert "idx_usage_payload_archive_refs_archive" in {
        row["name"]
        for row in db.query_all("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    db.close()


def test_discord_self_service_validation_requires_enabled_config_fields(tmp_path: Path):
    db = Database(str(tmp_path / "test.db"))
    db.init_schema()
    config = AppConfig.model_validate({"self_service": {"discord": {"enabled": True, "client_id": "client"}}})

    with pytest.raises(ValueError, match="self_service.discord.client_secret"):
        validate_discord_self_service_config(db, config)
    with pytest.raises(ValueError, match="self_service.discord.default_group_id"):
        validate_discord_self_service_config(db, config)

    db.close()


def test_discord_self_service_validation_requires_existing_active_default_group(tmp_path: Path):
    db = Database(str(tmp_path / "test.db"))
    db.init_schema()
    config = AppConfig.model_validate(
        {
            "self_service": {
                "discord": {
                    "enabled": True,
                    "client_id": "client",
                    "client_secret": "secret",
                    "required_guild_id": "200000000000000001",
                    "default_group_id": 1,
                    "session_secret": "session-secret",
                }
            }
        }
    )

    with pytest.raises(ValueError, match="existing enabled user_groups.id"):
        validate_discord_self_service_config(db, config)

    db.execute(
        "INSERT INTO user_groups(name, is_active, created_at) VALUES (?, 0, ?)",
        ("disabled", utc_now_iso()),
    )
    with pytest.raises(ValueError, match="enabled user group"):
        validate_discord_self_service_config(db, config)

    db.execute("UPDATE user_groups SET is_active = 1 WHERE id = 1")
    validate_discord_self_service_config(db, config)
    db.close()


@pytest.mark.parametrize(
    "required_guild_id",
    [
        "guild",
        "0200000000000000001",
        str(1 << 64),
    ],
)
def test_discord_self_service_validation_rejects_invalid_required_guild_id(
    tmp_path: Path,
    required_guild_id: str,
):
    db = Database(str(tmp_path / "test.db"))
    db.init_schema()
    db.execute(
        "INSERT INTO user_groups(name, is_active, created_at) VALUES (?, 1, ?)",
        ("enabled", utc_now_iso()),
    )
    config = AppConfig.model_validate(
        {
            "self_service": {
                "discord": {
                    "enabled": True,
                    "client_id": "client",
                    "client_secret": "secret",
                    "required_guild_id": required_guild_id,
                    "default_group_id": 1,
                    "session_secret": "session-secret",
                }
            }
        }
    )

    with pytest.raises(ValueError, match="valid Discord guild snowflake"):
        validate_discord_self_service_config(db, config)

    db.close()


def test_usage_logs_unique_constraint_migration_allows_retry_attempts(tmp_path: Path):
    db_path = tmp_path / "old-schema.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key_hash TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL UNIQUE,
            tier TEXT NOT NULL DEFAULT 'normal',
            rate_limit_count INTEGER NOT NULL DEFAULT 20,
            rate_limit_window_seconds INTEGER NOT NULL DEFAULT 3600,
            created_at TEXT NOT NULL
        );
        CREATE TABLE usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            estimated_anlas_cost INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO users(api_key_hash, name, created_at)
        VALUES ('hash', 'user', '2026-01-01T00:00:00+00:00');
        INSERT INTO usage_logs(request_id, user_id, action, estimated_anlas_cost, status, created_at)
        VALUES ('retry-request', 1, 'generate', 0, 'failed', '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    db = Database(str(db_path))
    db.init_schema()
    db.execute(
        """
        INSERT INTO usage_logs(request_id, attempt_number, user_id, action, estimated_anlas_cost, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("retry-request", 1, 1, "generate", 0, "running", "2026-01-01T00:00:01+00:00"),
    )

    rows = db.query_all("SELECT request_id, attempt_number FROM usage_logs ORDER BY attempt_number")
    assert [row["attempt_number"] for row in rows] == [0, 1]
    table_sql = db.query_one("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'usage_logs'")["sql"]
    assert "UNIQUE(request_id, attempt_number)" in table_sql
    indexes = {
        row["name"]
        for row in db.query_all("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'usage_logs'")
    }
    assert {"idx_usage_created_at", "idx_usage_action_created", "idx_usage_status_created"} <= indexes
    columns = {row["name"] for row in db.query_all("PRAGMA table_info(usage_logs)")}
    assert {
        "request_payload_encoding",
        "request_payload_blob",
        "request_payload_bytes",
        "request_payload_compressed_bytes",
    } <= columns
    db.close()


def test_usage_logs_unique_constraint_migration_preserves_archive_refs(tmp_path: Path):
    db_path = tmp_path / "old-schema-with-refs.db"
    _create_legacy_usage_logs_schema_with_archive_ref(db_path)

    db = Database(str(db_path))
    db.init_schema()

    refs = db.query_all("SELECT log_id, archive_id, payload_key, payload_bytes FROM usage_log_payload_archive_refs")
    assert [dict(row) for row in refs] == [{"log_id": 1, "archive_id": 1, "payload_key": "1", "payload_bytes": 123}]
    assert db.query_all("PRAGMA foreign_key_check") == []

    db.init_schema()
    assert db.query_one("SELECT COUNT(*) AS count FROM usage_log_payload_archive_refs")["count"] == 1
    db.close()


def test_usage_logs_unique_constraint_migration_rolls_back_on_midway_failure(
    tmp_path: Path,
    monkeypatch,
):
    from app.database import migrations
    from app.database import schema

    db_path = tmp_path / "old-schema-rollback.db"
    _create_legacy_usage_logs_schema_with_archive_ref(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for column, definition in schema.USAGE_LOGS_COLUMNS:
            migrations.add_column_if_missing(conn, "usage_logs", column, definition)
        before_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'usage_logs'"
        ).fetchone()["sql"]
        monkeypatch.setattr(
            migrations,
            "USAGE_LOGS_INDEX_SQL",
            (*migrations.USAGE_LOGS_INDEX_SQL, "CREATE INDEX broken_usage_logs_index ON missing_table(id)"),
        )

        with pytest.raises(sqlite3.OperationalError):
            migrations.migrate_usage_logs_unique_constraint(conn)

        after_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'usage_logs'"
        ).fetchone()["sql"]
        assert after_sql == before_sql
        assert conn.execute("SELECT COUNT(*) AS count FROM usage_logs").fetchone()["count"] == 1
        assert conn.execute("SELECT COUNT(*) AS count FROM usage_log_payload_archive_refs").fetchone()["count"] == 1
        assert conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'usage_logs_new'").fetchone() is None
    finally:
        conn.close()


def _create_legacy_usage_logs_schema_with_archive_ref(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key_hash TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                estimated_anlas_cost INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                request_payload TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE usage_log_payload_archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                archive_date TEXT NOT NULL,
                part_number INTEGER NOT NULL,
                compression TEXT NOT NULL,
                compression_level INTEGER NOT NULL,
                payload_count INTEGER NOT NULL,
                raw_bytes INTEGER NOT NULL,
                compressed_bytes INTEGER NOT NULL,
                payload_sha256 TEXT NOT NULL,
                payload_blob BLOB NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(archive_date, part_number)
            );
            CREATE TABLE usage_log_payload_archive_refs (
                log_id INTEGER PRIMARY KEY REFERENCES usage_logs(id) ON DELETE CASCADE,
                archive_id INTEGER NOT NULL REFERENCES usage_log_payload_archives(id) ON DELETE CASCADE,
                payload_key TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL
            );
            INSERT INTO users(api_key_hash, name, created_at)
            VALUES ('hash', 'user', '2026-01-01T00:00:00+00:00');
            INSERT INTO usage_logs(request_id, user_id, action, estimated_anlas_cost, status, request_payload, created_at)
            VALUES ('archived-retry-request', 1, 'generate', 0, 'success', '{"input":"legacy"}', '2026-01-01T00:00:00+00:00');
            INSERT INTO usage_log_payload_archives (
                id, archive_date, part_number, compression, compression_level, payload_count,
                raw_bytes, compressed_bytes, payload_sha256, payload_blob, created_at
            )
            VALUES (1, '2026-01-01', 1, 'zlib', 6, 1, 10, 10, 'sha', X'00', '2026-01-01T00:00:00+00:00');
            INSERT INTO usage_log_payload_archive_refs(log_id, archive_id, payload_key, payload_bytes)
            VALUES (1, 1, '1', 123);
            """
        )
        conn.commit()
    finally:
        conn.close()
