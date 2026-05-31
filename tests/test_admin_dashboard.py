from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from helpers import write_test_config


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
        assert "<strong>3</strong>" in today_metric
        assert 'id="request-trends"' in dashboard.text
        trend_json = dashboard.text.split('<script id="request-trends" type="application/json">', 1)[1].split("</script>", 1)[0]
        trends = json.loads(trend_json)
        assert trends["today"]["totals"] == {"requests": 3, "failed": 1, "rejected": 1}
        assert sum(trends["today"]["series"]["requests"]) == 3
        assert sum(trends["week"]["series"]["failed"]) == 1
        assert sum(trends["month"]["series"]["rejected"]) == 1
