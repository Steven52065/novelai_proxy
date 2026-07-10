from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.database import Database, utc_now_iso
from helpers import write_test_config


def test_health_admin_create_user_and_subscription(tmp_path: Path, monkeypatch):
    config_path = write_test_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    try:
        from app.main import app

        with TestClient(app) as client:
            assert client.get("/health").json()["status"] == "ok"

            create_resp = client.post(
                "/admin/api/users",
                auth=("admin", "admin123"),
                json={"name": "alice", "tier": "normal", "anlas_total": 100},
            )
            assert create_resp.status_code == 200
            api_key = create_resp.json()["api_key"]
            assert api_key.startswith("nai_proxy_")

            sub_resp = client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"})
            assert sub_resp.status_code == 200
            body = sub_resp.json()
            assert body["proxyQuota"]["total"] == 100
            assert body["proxyQuota"]["available"] == 100

        log_text = (tmp_path / "logs" / "novelai_proxy.log").read_text(encoding="utf-8")
        assert "http request completed method=GET path=/health status=200" in log_text
        assert "http request details method=GET path=/health" in log_text
    finally:
        monkeypatch.delenv("NOVELAI_PROXY_CONFIG", raising=False)


def test_sensitive_pages_disable_browser_caching(tmp_path: Path, monkeypatch):
    config_path = write_test_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    try:
        from app.main import app

        with TestClient(app) as client:
            login = client.get("/admin/login")
            assert login.headers["cache-control"] == "no-store, private"
            assert login.headers["pragma"] == "no-cache"

            health = client.get("/health")
            assert "cache-control" not in health.headers
    finally:
        monkeypatch.delenv("NOVELAI_PROXY_CONFIG", raising=False)


def test_service_worker_never_caches_navigation_responses():
    source = (Path(__file__).parents[1] / "static" / "sw.js").read_text(encoding="utf-8")
    navigation_block = source.split("if (isNavigation(request))", 1)[1].split("if (isStaticAsset(url))", 1)[0]
    assert "event.respondWith(fetch(request))" in navigation_block
    assert "cache.put(request" not in navigation_block
    assert 'caches.match("/admin")' not in source


def test_startup_reclaims_orphan_reserved_quota_and_daily_usage(tmp_path: Path, monkeypatch):
    config_path = write_test_config(tmp_path)
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    db.init_schema()
    now = utc_now_iso()
    try:
        cursor = db.execute(
            """
            INSERT INTO users (
                api_key_hash, name, is_active, free_small_daily_limit_enabled,
                free_small_daily_limit, created_at
            )
            VALUES (?, ?, 1, 1, 10, ?)
            """,
            ("hash-orphan", "orphan-user", now),
        )
        user_id = int(cursor.lastrowid)
        db.execute(
            """
            INSERT INTO user_anlas_quota (
                user_id, total, used, reserved, reset_period, reset_day, last_reset_at, created_at
            )
            VALUES (?, 20, 5, 7, 'never', 0, ?, ?)
            """,
            (user_id, now, now),
        )
        db.execute(
            """
            INSERT INTO free_small_daily_usage (
                user_id, window_start, used, reserved, created_at, updated_at
            )
            VALUES (?, ?, 3, 4, ?, ?)
            """,
            (user_id, "2026-01-01T00:00:00+08:00", now, now),
        )
    finally:
        db.close()

    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    try:
        from app.main import app

        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            quota = client.app.state.db.query_one(
                "SELECT total, used, reserved FROM user_anlas_quota WHERE user_id = ?",
                (user_id,),
            )
            daily_usage = client.app.state.db.query_one(
                "SELECT used, reserved FROM free_small_daily_usage WHERE user_id = ?",
                (user_id,),
            )
            assert dict(quota) == {"total": 20, "used": 5, "reserved": 0}
            assert dict(daily_usage) == {"used": 3, "reserved": 0}
    finally:
        monkeypatch.delenv("NOVELAI_PROXY_CONFIG", raising=False)
