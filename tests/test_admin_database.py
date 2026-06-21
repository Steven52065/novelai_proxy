from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.database import Database
from app.database import utc_now_iso, validate_discord_self_service_config
from helpers import write_test_config


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
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level,
                    request_payload, output_files, image_urls, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    user_id,
                    "generate",
                    0,
                    "success",
                    "INFO",
                    large_payload,
                    '["image.png"]',
                    '[{"url":"https://files.catbox.moe/image.png"}]',
                    created_at,
                ),
            )
        app.state.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, user_id, action, estimated_anlas_cost, status, log_level,
                request_payload, output_files, image_urls, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old-large-image-urls",
                user_id,
                "generate",
                0,
                "success",
                "INFO",
                None,
                "[]",
                '[{"url":"https://files.catbox.moe/' + ("b" * 2048) + '.png"}]',
                old_time,
            ),
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
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level, request_payload, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, user_id, "generate", 0, "success", "INFO", payload, created_at),
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
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level, request_payload, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f"archive-part-{index}", user_id, "generate", 0, "success", "INFO", f'{{"index":{index}}}', "2026-05-10T00:00:00+00:00"),
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
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level, request_payload, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, user_id, "generate", 0, status, "INFO", f'{{"request_id":"{request_id}"}}', "2026-05-10T00:00:00+00:00"),
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

        form_resp = client.post("/admin/database/vacuum", follow_redirects=False)
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
                    "required_guild_id": "guild",
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
    db.close()
