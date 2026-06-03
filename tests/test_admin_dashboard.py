from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

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
