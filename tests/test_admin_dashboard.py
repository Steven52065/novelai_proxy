from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helpers import write_test_config, write_test_config_with_upstreams


def test_admin_login_page(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"}, follow_redirects=False)
        assert login.status_code == 303
        assert "novelai_proxy_admin" in login.headers["set-cookie"]
        assert "Max-Age=2592000" in login.headers["set-cookie"]

        dashboard = client.get("/admin")
        assert dashboard.status_code == 200
        assert "novelai_proxy_admin" in dashboard.headers["set-cookie"]
        assert "Max-Age=2592000" in dashboard.headers["set-cookie"]
        assert "仪表盘" in dashboard.text

def test_admin_invalid_session_cookie_is_not_refreshed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        client.cookies.set("novelai_proxy_admin", "invalid")
        dashboard = client.get("/admin", follow_redirects=False)

        assert dashboard.status_code == 303
        assert "set-cookie" not in dashboard.headers

def test_admin_dashboard_shows_request_trend_stats(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "trend-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        now = datetime.now(timezone.utc).isoformat()
        local_today_start = datetime.now(timezone(timedelta(hours=8))).replace(hour=0, minute=0, second=0, microsecond=0)
        previous_local_day = (local_today_start - timedelta(minutes=30)).astimezone(timezone.utc).isoformat()
        for request_id, status in (
            ("trend-success", "success"),
            ("trend-failed", "failed"),
            ("trend-rejected", "rejected"),
        ):
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, user_id, "generate", 0, status, "INFO", now),
            )
        app.state.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("trend-previous-local-day", user_id, "generate", 0, "success", "INFO", previous_local_day),
        )

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        dashboard = client.get("/admin")

        assert dashboard.status_code == 200
        assert "请求数量趋势" in dashboard.text
        assert "今日生成请求" in dashboard.text
        today_metric = dashboard.text.split("今日生成请求", 1)[1].split("</div>", 1)[0]
        # 使用 COUNT(DISTINCT request_id) 后，3个不同的 request_id 应该统计为 3
        # HTML 中的格式是 <strong id="stat-today-requests">3</strong>
        assert 'id="stat-today-requests">3</strong>' in today_metric
        assert 'id="request-trends"' in dashboard.text
        trend_json = dashboard.text.split('<script id="request-trends" type="application/json">', 1)[1].split("</script>", 1)[0]
        trends = json.loads(trend_json)
        # COUNT(DISTINCT request_id) 统计请求数，失败、拒绝和重试成功用 COUNT(*) 统计记录数
        assert trends["today"]["totals"] == {"requests": 3, "failed": 1, "rejected": 1, "retry_success": 0}
        assert sum(trends["today"]["series"]["requests"]) == 3
        assert sum(trends["week"]["series"]["failed"]) == 1
        assert sum(trends["month"]["series"]["rejected"]) == 1
        assert sum(trends["today"]["series"]["retry_success"]) == 0


def test_admin_request_trends_can_filter_by_upstream(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"])))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "trend-upstream-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        now = datetime.now(timezone.utc).isoformat()
        for request_id, upstream_id, status, is_retry_success in (
            ("trend-a-success", "opus-a", "success", 0),
            ("trend-a-failed", "opus-a", "failed", 0),
            ("trend-b-success", "opus-b", "success", 0),
            ("trend-a-retry-success", "opus-a", "success", 1),
        ):
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level, upstream_id, is_retry_success, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, user_id, "generate", 0, status, "INFO", upstream_id, is_retry_success, now),
            )

        client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        dashboard = client.get("/admin")
        assert dashboard.status_code == 200
        assert '<option value="opus-a">opus-a</option>' in dashboard.text
        assert '<option value="opus-b">opus-b</option>' in dashboard.text

        all_resp = client.get("/admin/api/request-trends", auth=("admin", "admin123"))
        filtered_resp = client.get("/admin/api/request-trends?upstream_id=opus-a", auth=("admin", "admin123"))

        assert all_resp.status_code == 200
        assert filtered_resp.status_code == 200
        assert all_resp.json()["today"]["totals"] == {"requests": 4, "failed": 1, "rejected": 0, "retry_success": 1}
        assert filtered_resp.json()["today"]["totals"] == {"requests": 3, "failed": 1, "rejected": 0, "retry_success": 1}


def test_admin_dashboard_deduplicates_cross_hour_retry_attempts_for_day_counts(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a"])))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "cross-hour-retry-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        local_today_start = datetime.now(timezone(timedelta(hours=8))).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        first_attempt_at = (local_today_start + timedelta(hours=1, minutes=10)).astimezone(timezone.utc).isoformat()
        retry_attempt_at = (local_today_start + timedelta(hours=2, minutes=10)).astimezone(timezone.utc).isoformat()
        separate_request_at = (local_today_start + timedelta(hours=3, minutes=10)).astimezone(timezone.utc).isoformat()

        for request_id, attempt_number, status, is_retry_success, created_at in (
            ("cross-hour-retry", 0, "failed", 0, first_attempt_at),
            ("cross-hour-retry", 1, "success", 1, retry_attempt_at),
            ("separate-today-request", 0, "success", 0, separate_request_at),
        ):
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, attempt_number, user_id, action, estimated_anlas_cost,
                    final_anlas_cost, status, log_level, upstream_id, is_retry_success, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    attempt_number,
                    user_id,
                    "generate",
                    0,
                    0 if status == "success" else None,
                    status,
                    "INFO",
                    "opus-a",
                    is_retry_success,
                    created_at,
                ),
            )

        dashboard_resp = client.get("/admin/api/dashboard", auth=("admin", "admin123"))
        trend_resp = client.get("/admin/api/request-trends?upstream_id=opus-a", auth=("admin", "admin123"))

        assert dashboard_resp.status_code == 200
        assert dashboard_resp.json()["stats"]["today_requests"] == 2
        assert trend_resp.status_code == 200
        trends = trend_resp.json()
        assert trends["week"]["totals"]["requests"] == 2
        assert trends["month"]["totals"]["requests"] == 2
        assert trends["week"]["totals"]["failed"] == 1
        assert trends["week"]["totals"]["retry_success"] == 1


def test_dashboard_hourly_stats_track_usage_log_changes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"])))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "hourly-stats-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        now = datetime.now(timezone.utc).isoformat()
        db = app.state.db

        db.execute(
            """
            INSERT INTO usage_logs (
                request_id, attempt_number, user_id, action, estimated_anlas_cost,
                status, log_level, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("hourly-success", 0, user_id, "generate", 5, "queued", "INFO", now),
        )
        db.execute(
            """
            UPDATE usage_logs
            SET status = 'running', upstream_id = ?
            WHERE request_id = ? AND attempt_number = ?
            """,
            ("opus-a", "hourly-success", 0),
        )
        db.execute(
            """
            UPDATE usage_logs
            SET status = 'success', final_anlas_cost = ?
            WHERE request_id = ? AND attempt_number = ?
            """,
            (5, "hourly-success", 0),
        )

        db.execute(
            """
            INSERT INTO usage_logs (
                request_id, attempt_number, user_id, action, estimated_anlas_cost,
                status, log_level, upstream_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("hourly-retry", 0, user_id, "generate", 7, "failed", "ERROR", "opus-a", now),
        )
        db.execute(
            """
            INSERT INTO usage_logs (
                request_id, attempt_number, user_id, action, estimated_anlas_cost,
                status, log_level, upstream_id, is_retry_success, final_anlas_cost, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("hourly-retry", 1, user_id, "generate", 7, "success", "INFO", "opus-b", 1, 7, now),
        )

        all_row = _dashboard_hourly_totals(db, "__all__")
        upstream_a = _dashboard_hourly_totals(db, "opus-a")
        upstream_b = _dashboard_hourly_totals(db, "opus-b")

        assert all_row == {
            "requests": 2,
            "success": 2,
            "failed": 1,
            "rejected": 0,
            "retry_success": 1,
            "anlas": 12,
        }
        assert upstream_a == {
            "requests": 2,
            "success": 1,
            "failed": 1,
            "rejected": 0,
            "retry_success": 0,
            "anlas": 5,
        }
        assert upstream_b == {
            "requests": 1,
            "success": 1,
            "failed": 0,
            "rejected": 0,
            "retry_success": 1,
            "anlas": 7,
        }

        filtered_resp = client.get("/admin/api/request-trends?upstream_id=opus-b", auth=("admin", "admin123"))
        assert filtered_resp.status_code == 200
        assert filtered_resp.json()["today"]["totals"] == {
            "requests": 1,
            "failed": 0,
            "rejected": 0,
            "retry_success": 1,
        }


def test_dashboard_hourly_stats_can_be_rebuilt_from_usage_logs(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a"])))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "hourly-rebuild-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        now = datetime.now(timezone.utc).isoformat()
        db = app.state.db
        for attempt_number, status, is_retry_success in (
            (0, "failed", 0),
            (1, "success", 1),
        ):
            db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, attempt_number, user_id, action, estimated_anlas_cost,
                    final_anlas_cost, status, log_level, upstream_id, is_retry_success, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "hourly-rebuild",
                    attempt_number,
                    user_id,
                    "generate",
                    4,
                    4 if status == "success" else None,
                    status,
                    "INFO",
                    "opus-a",
                    is_retry_success,
                    now,
                ),
            )

        db.execute("DELETE FROM dashboard_hourly_stats")
        db.execute("DELETE FROM dashboard_hourly_request_refs")
        assert _dashboard_hourly_totals(db, "__all__") == {
            "requests": 0,
            "success": 0,
            "failed": 0,
            "rejected": 0,
            "retry_success": 0,
            "anlas": 0,
        }

        db.rebuild_dashboard_hourly_stats()

        assert _dashboard_hourly_totals(db, "__all__") == {
            "requests": 1,
            "success": 1,
            "failed": 1,
            "rejected": 0,
            "retry_success": 1,
            "anlas": 4,
        }
        assert _dashboard_hourly_totals(db, "opus-a") == {
            "requests": 1,
            "success": 1,
            "failed": 1,
            "rejected": 0,
            "retry_success": 1,
            "anlas": 4,
        }


def _dashboard_hourly_totals(db, upstream_id: str) -> dict[str, int]:
    row = db.query_one(
        """
        SELECT COALESCE(SUM(request_count), 0) AS requests,
               COALESCE(SUM(success_count), 0) AS success,
               COALESCE(SUM(failed_count), 0) AS failed,
               COALESCE(SUM(rejected_count), 0) AS rejected,
               COALESCE(SUM(retry_success_count), 0) AS retry_success,
               COALESCE(SUM(anlas_cost), 0) AS anlas
        FROM dashboard_hourly_stats
        WHERE upstream_id = ?
        """,
        (upstream_id,),
    )
    return {key: int(row[key] or 0) for key in row.keys()}


def test_admin_dashboard_snapshot_api_combines_ui_data(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "snapshot-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        app.state.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, user_id, action, estimated_anlas_cost, final_anlas_cost, status, log_level, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("snapshot-success", user_id, "generate", 5, 5, "success", "INFO", datetime.now(timezone.utc).isoformat()),
        )

        resp = client.get("/admin/api/dashboard", auth=("admin", "admin123"))

        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "dashboard.snapshot"
        assert body["stats"]["total_users"] == 1
        assert body["stats"]["today_requests"] == 1
        assert body["stats"]["total_anlas"] == 5
        assert "queue" in body
        assert "upstream_weights" in body
        assert body["request_trends"] is None


def test_admin_dashboard_websocket_sends_snapshot_for_session(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        with client.websocket_connect("/admin/ws/dashboard", headers={"Origin": "http://testserver"}) as websocket:
            body = websocket.receive_json()

        assert body["type"] == "dashboard.snapshot"
        assert "stats" in body
        assert "queue" in body
        assert "upstream_weights" in body
        assert body["request_trends"] is None


def test_admin_dashboard_websocket_accepts_forwarded_origin(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        with client.websocket_connect(
            "/admin/ws/dashboard",
            headers={
                "Origin": "https://admin.example.com",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "admin.example.com",
            },
        ) as websocket:
            body = websocket.receive_json()

        assert body["type"] == "dashboard.snapshot"


def test_admin_dashboard_websocket_accepts_same_host_https_origin_without_forwarded_proto(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        with client.websocket_connect(
            "/admin/ws/dashboard",
            headers={
                "Host": "admin.example.com",
                "Origin": "https://admin.example.com",
            },
        ) as websocket:
            body = websocket.receive_json()

        assert body["type"] == "dashboard.snapshot"


def test_admin_dashboard_websocket_sends_heartbeat_when_idle(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    import app.admin.dashboard as dashboard_module
    from app.main import app

    monkeypatch.setattr(dashboard_module, "DASHBOARD_WS_HEARTBEAT_SECONDS", 0.05)

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        with client.websocket_connect("/admin/ws/dashboard", headers={"Origin": "http://testserver"}) as websocket:
            assert websocket.receive_json()["type"] == "dashboard.snapshot"
            heartbeat = websocket.receive_json()

        assert heartbeat["type"] == "dashboard.heartbeat"
        assert "stats" not in heartbeat
        assert "queue" not in heartbeat
        assert "upstream_weights" not in heartbeat


def test_admin_dashboard_websocket_sends_snapshot_after_data_change(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        with client.websocket_connect("/admin/ws/dashboard", headers={"Origin": "http://testserver"}) as websocket:
            first = websocket.receive_json()
            assert first["type"] == "dashboard.snapshot"
            assert first["stats"]["total_users"] == 0

            create_resp = client.post(
                "/admin/api/users",
                auth=("admin", "admin123"),
                json={"name": "ws-user", "tier": "normal", "anlas_total": 100},
            )
            assert create_resp.status_code == 200
            changed = websocket.receive_json()

        assert changed["type"] == "dashboard.snapshot"
        assert changed["stats"]["total_users"] == 1


def test_admin_dashboard_websocket_rejects_cross_origin(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/admin/ws/dashboard",
                headers={"Origin": "http://evil.example"},
            ):
                pass

        assert exc_info.value.code == 1008


def test_admin_queue_status_can_filter_by_upstream(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"])))
    from app.main import app

    class MultiUpstreamQueueSnapshot:
        def qsize(self):
            return 2

        def snapshot(self):
            return {
                "queue_size": 2,
                "running": None,
                "running_items": [
                    {
                        "request_id": "running-a",
                        "user_id": 1,
                        "action": "generate",
                        "tier": "normal",
                        "upstream_id": "opus-a",
                        "estimated_anlas_cost": 0,
                        "priority": 10,
                        "position": 0,
                        "status": "running",
                        "queued_seconds": 3,
                    },
                    {
                        "request_id": "running-b",
                        "user_id": 1,
                        "action": "generate",
                        "tier": "normal",
                        "upstream_id": "opus-b",
                        "estimated_anlas_cost": 0,
                        "priority": 10,
                        "position": 0,
                        "status": "running",
                        "queued_seconds": 3,
                    },
                ],
                "queued": [
                    {
                        "request_id": "queued-a",
                        "user_id": 1,
                        "action": "generate",
                        "tier": "normal",
                        "upstream_id": "opus-a",
                        "estimated_anlas_cost": 0,
                        "priority": 10,
                        "position": 1,
                        "status": "queued",
                        "queued_seconds": 8,
                    },
                    {
                        "request_id": "queued-b",
                        "user_id": 1,
                        "action": "generate",
                        "tier": "normal",
                        "upstream_id": "opus-b",
                        "estimated_anlas_cost": 0,
                        "priority": 10,
                        "position": 2,
                        "status": "queued",
                        "queued_seconds": 8,
                    },
                ],
            }

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "queue-upstream-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        now = datetime.now(timezone.utc).isoformat()
        for request_id in ("running-a", "running-b", "queued-a", "queued-b"):
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, user_id, "generate", 0, "queued", "INFO", now),
            )
        app.state.proxy_queue = MultiUpstreamQueueSnapshot()

        resp = client.get("/admin/api/queue?upstream_id=opus-b", auth=("admin", "admin123"))

        assert resp.status_code == 200
        body = resp.json()
        assert body["queue_size"] == 1
        assert [item["request_id"] for item in body["running_items"]] == ["running-b"]
        assert body["running"]["request_id"] == "running-b"
        assert [item["request_id"] for item in body["queued"]] == ["queued-b"]
        assert body["queued"][0]["position"] == 1
