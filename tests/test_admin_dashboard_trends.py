from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from dashboard_helpers import _unique_request_count
from helpers import write_test_config, write_test_config_with_upstreams


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
        assert trends["today"]["totals"]["requests"] == 2
        assert trends["week"]["totals"]["requests"] == 2
        assert trends["month"]["totals"]["requests"] == 2
        assert trends["week"]["totals"]["failed"] == 1
        assert trends["week"]["totals"]["retry_success"] == 1

def test_admin_request_trend_totals_deduplicate_across_range_buckets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a"])))
    from app.main import app

    display_timezone = timezone(timedelta(hours=8))

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "range-dedupe-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        today_start = datetime.now(display_timezone).replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())
        week_end = week_start + timedelta(days=7)
        month_start = today_start.replace(day=1)
        month_end = (
            month_start.replace(year=month_start.year + 1, month=1)
            if month_start.month == 12
            else month_start.replace(month=month_start.month + 1)
        )
        attempts = [
            ("today-cross-bucket", 0, "failed", 0, today_start + timedelta(hours=1, minutes=10)),
            ("today-cross-bucket", 1, "success", 1, today_start + timedelta(hours=2, minutes=10)),
            ("today-single", 0, "success", 0, today_start + timedelta(hours=3, minutes=10)),
            ("week-cross-bucket", 0, "failed", 0, week_start + timedelta(hours=1, minutes=10)),
            ("week-cross-bucket", 1, "success", 1, week_start + timedelta(days=1, hours=1, minutes=10)),
            ("month-cross-bucket", 0, "failed", 0, month_start + timedelta(hours=1, minutes=10)),
            ("month-cross-bucket", 1, "success", 1, month_start + timedelta(days=1, hours=1, minutes=10)),
        ]

        for request_id, attempt_number, status, is_retry_success, local_created_at in attempts:
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
                    local_created_at.astimezone(timezone.utc).isoformat(),
                ),
            )

        trend_resp = client.get("/admin/api/request-trends?upstream_id=opus-a", auth=("admin", "admin123"))

        assert trend_resp.status_code == 200
        trends = trend_resp.json()
        assert trends["today"]["totals"]["requests"] == _unique_request_count(attempts, today_start, today_start + timedelta(days=1))
        assert trends["week"]["totals"]["requests"] == _unique_request_count(attempts, week_start, week_end)
        assert trends["month"]["totals"]["requests"] == _unique_request_count(attempts, month_start, month_end)
