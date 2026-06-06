from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from dashboard_helpers import _dashboard_hourly_totals
from helpers import write_test_config_with_upstreams


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


def _unique_request_count(attempts: list[tuple[str, int, str, int, datetime]], start: datetime, end: datetime) -> int:
    return len({request_id for request_id, _, _, _, created_at in attempts if start <= created_at < end})
