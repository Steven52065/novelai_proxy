from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.novelai_endpoints import GENERATE_IMAGE_ENDPOINT
from app.usage_logs import UsageLogCreate
from helpers import PAYLOAD, FakeUpstream, write_test_config


def test_admin_can_replay_rejected_generate_without_quota_charge(tmp_path: Path, monkeypatch):
    config_path = write_test_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    paid_payload = PAYLOAD | {
        "parameters": PAYLOAD["parameters"] | {"width": 1024, "height": 1024, "steps": 50}
    }
    with TestClient(app) as client:
        fake_upstream = FakeUpstream()
        app.state.upstream = fake_upstream
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "replay-low-quota", "tier": "normal", "anlas_total": 1},
        )
        user_id = create_resp.json()["user_id"]
        api_key = create_resp.json()["api_key"]

        rejected = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=paid_payload)
        assert rejected.status_code == 402
        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        rejected_log = next(row for row in logs if row["status"] == "rejected")

        replay = client.post(f"/admin/api/logs/{rejected_log['request_id']}/replay", auth=("admin", "admin123"))

        assert replay.status_code == 200
        body = replay.json()
        assert body["source_request_id"] == rejected_log["request_id"]
        assert body["replay_request_id"] != rejected_log["request_id"]
        assert body["images"][0]["filename"] == "image.png"
        assert body["images"][0]["data_url"].startswith("data:image/png;base64,")
        assert fake_upstream.last_post_binary_url == GENERATE_IMAGE_ENDPOINT
        assert fake_upstream.last_post_binary_payload == paid_payload

        quota = app.state.quota_manager.get_snapshot(user_id)
        assert quota.used == 0
        assert quota.reserved == 0

        replay_log = app.state.db.query_one(
            "SELECT action, status, estimated_anlas_cost, final_anlas_cost FROM usage_logs WHERE request_id = ?",
            (body["replay_request_id"],),
        )
        assert replay_log["action"] == "replay:generate"
        assert replay_log["status"] == "success"
        assert replay_log["estimated_anlas_cost"] == 0
        assert replay_log["final_anlas_cost"] == 0


def test_admin_replay_by_log_id_uses_exact_retry_attempt(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        fake_upstream = FakeUpstream()
        app.state.upstream = fake_upstream
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "replay-retry-attempt", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        app.state.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, attempt_number, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, log_level, upstream_id, request_payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "same-request",
                0,
                user_id,
                "generate",
                "nai-diffusion-3",
                832,
                1216,
                23,
                1,
                0,
                "failed",
                "ERROR",
                "default",
                '{"input":"first attempt","model":"nai-diffusion-3","parameters":{}}',
                "2026-05-27T00:00:00+00:00",
            ),
        )
        app.state.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, attempt_number, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, log_level, upstream_id, request_payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "same-request",
                1,
                user_id,
                "generate",
                "nai-diffusion-3",
                832,
                1216,
                23,
                1,
                0,
                "success",
                "INFO",
                "default",
                '{"input":"second attempt","model":"nai-diffusion-3","parameters":{}}',
                "2026-05-27T00:00:01+00:00",
            ),
        )
        source = app.state.db.query_one(
            "SELECT id FROM usage_logs WHERE request_id = ? AND attempt_number = ?",
            ("same-request", 1),
        )

        replay = client.post(f"/admin/api/logs/by-id/{source['id']}/replay", auth=("admin", "admin123"))

        assert replay.status_code == 200
        body = replay.json()
        assert body["source_log_id"] == source["id"]
        assert body["source_request_id"] == "same-request"
        assert body["source_attempt_number"] == 1
        assert fake_upstream.last_post_binary_payload["input"] == "second attempt"


def test_admin_replay_by_log_id_reads_archived_payload(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        fake_upstream = FakeUpstream()
        app.state.upstream = fake_upstream
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "replay-archived-payload", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        app.state.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, log_level, upstream_id, request_payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "archived-replay-source",
                user_id,
                "generate",
                "nai-diffusion-3",
                832,
                1216,
                23,
                1,
                0,
                "success",
                "INFO",
                "default",
                '{"input":"archived replay","model":"nai-diffusion-3","parameters":{}}',
                "2026-05-10T00:00:00+00:00",
            ),
        )
        app.state.payload_archive_service.archive_due_payloads(
            now=datetime(2026, 5, 22, 12, tzinfo=timezone.utc),
            hot_days=7,
        )
        source = app.state.db.query_one("SELECT id, request_payload FROM usage_logs WHERE request_id = ?", ("archived-replay-source",))
        assert source["request_payload"] is None

        replay = client.post(f"/admin/api/logs/by-id/{source['id']}/replay", auth=("admin", "admin123"))

        assert replay.status_code == 200
        assert fake_upstream.last_post_binary_payload["input"] == "archived replay"


def test_admin_logs_payload_detail_and_replay_read_hot_compressed_payload(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "NOVELAI_PROXY_CONFIG",
        str(write_test_config(tmp_path, hot_payload_enabled=True, hot_payload_min_bytes=100)),
    )
    from app.main import app

    hot_payload = PAYLOAD | {"input": "hot compressed prompt " * 500}
    with TestClient(app) as client:
        fake_upstream = FakeUpstream()
        app.state.upstream = fake_upstream
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "hot-compressed-replay", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        app.state.usage_logs.insert_queued(
            UsageLogCreate(
                request_id="hot-compressed-source",
                user_id=user_id,
                action="generate",
                model="nai-diffusion-3",
                width=832,
                height=1216,
                steps=23,
                n_samples=1,
                estimated_anlas_cost=0,
                request_payload=hot_payload,
            )
        )
        source = app.state.db.query_one("SELECT * FROM usage_logs WHERE request_id = ?", ("hot-compressed-source",))
        assert source["request_payload"] is None
        assert source["request_payload_encoding"] == "zlib"

        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        log = next(row for row in logs if row["request_id"] == "hot-compressed-source")
        assert log["request_payload"] is None
        assert log["has_request_payload"] is True
        assert log["payload_archived"] is False
        assert log["request_payload_bytes"] == source["request_payload_bytes"]

        payload = client.get(f"/admin/api/logs/by-id/{source['id']}/payload", auth=("admin", "admin123"))
        assert payload.status_code == 200
        assert payload.json()["request_payload"] == hot_payload

        replay = client.post(f"/admin/api/logs/by-id/{source['id']}/replay", auth=("admin", "admin123"))

        assert replay.status_code == 200
        assert fake_upstream.last_post_binary_url == GENERATE_IMAGE_ENDPOINT
        assert fake_upstream.last_post_binary_payload == hot_payload


def test_admin_logs_display_created_at_in_utc_plus_8(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "log-time-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        app.state.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, log_level, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("display-time-request", user_id, "generate", "nai-diffusion-3", 512, 768, 1, 1, 0, "success", "INFO", "2026-05-27T00:00:00+00:00"),
        )

        api_body = client.get("/admin/api/logs", auth=("admin", "admin123")).json()
        log = next(row for row in api_body["logs"] if row["request_id"] == "display-time-request")
        assert log["created_at"] == "2026-05-27T00:00:00+00:00"
        assert log["created_at_display"] == "2026-05-27 08:00:00 UTC+8"

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        logs_page = client.get("/admin/logs")
        assert logs_page.status_code == 200
        assert "2026-05-27 08:00:00 UTC+8" in logs_page.text

def test_admin_logs_filter_accepts_empty_user_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        logs_page = client.get("/admin/logs?user_id=&limit=100")
        assert logs_page.status_code == 200
        assert "使用日志审计" in logs_page.text

def test_admin_logs_api_supports_session_pagination(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "log-pagination-user", "tier": "normal", "anlas_total": 100},
        )
        user_id = create_resp.json()["user_id"]
        for index in range(5):
            app.state.db.execute(
                """
                INSERT INTO usage_logs (
                    request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (f"page-log-{index}", user_id, "generate", 0, "success", "INFO", f"2026-05-27T00:00:0{index}+00:00"),
            )

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        first_page = client.get("/admin/api/logs?limit=2")
        assert first_page.status_code == 200
        first_body = first_page.json()
        assert [row["request_id"] for row in first_body["logs"]] == ["page-log-4", "page-log-3"]
        assert first_body["has_more"] is True
        first_cursor = first_body["next_before_id"]
        assert first_cursor == first_body["logs"][-1]["id"]

        # A new log arriving between page fetches must not shift the cursor window.
        app.state.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("page-log-newer", user_id, "generate", 0, "success", "INFO", "2026-05-27T00:00:09+00:00"),
        )

        second_page = client.get(f"/admin/api/logs?limit=2&before_id={first_cursor}")
        assert second_page.status_code == 200
        second_body = second_page.json()
        assert [row["request_id"] for row in second_body["logs"]] == ["page-log-2", "page-log-1"]
        assert second_body["has_more"] is True
        second_cursor = second_body["next_before_id"]
        assert second_cursor == second_body["logs"][-1]["id"]

        final_page = client.get(f"/admin/api/logs?limit=2&before_id={second_cursor}")
        assert final_page.status_code == 200
        final_body = final_page.json()
        assert [row["request_id"] for row in final_body["logs"]] == ["page-log-0"]
        assert final_body["has_more"] is False
        assert final_body["next_before_id"] == final_body["logs"][-1]["id"]

        logs_page = client.get("/admin/logs?limit=2")
        assert logs_page.status_code == 200
        newest_page = client.get("/admin/api/logs?limit=2")
        newest_body = newest_page.json()
        assert f'data-next-before-id="{newest_body["next_before_id"]}"' in logs_page.text
        assert 'data-has-more="true"' in logs_page.text
        assert "window.localStorage" in logs_page.text
        assert "readStoredValue" in logs_page.text
        assert "IntersectionObserver" in logs_page.text


def test_admin_logs_api_filters_by_user_action_status_and_utc8_hour_range(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        alpha_id = _create_admin_user(client, "alpha-log-user")
        beta_id = _create_admin_user(client, "beta-log-user")
        _insert_usage_log(
            client,
            user_id=alpha_id,
            request_id="alpha-success",
            action="generate",
            status="success",
            created_at="2026-05-27T00:00:00+00:00",
        )
        _insert_usage_log(
            client,
            user_id=alpha_id,
            request_id="alpha-upscale",
            action="upscale",
            status="failed",
            created_at="2026-05-27T01:00:00+00:00",
        )
        _insert_usage_log(
            client,
            user_id=beta_id,
            request_id="beta-rejected",
            action="generate",
            status="rejected",
            created_at="2026-05-27T02:00:00+00:00",
        )

        by_user = client.get(f"/admin/api/logs?user_id={alpha_id}&limit=100", auth=("admin", "admin123"))
        assert by_user.status_code == 200
        assert {row["request_id"] for row in by_user.json()["logs"]} == {"alpha-success", "alpha-upscale"}

        by_action = client.get("/admin/api/logs?action=upscale&limit=100", auth=("admin", "admin123"))
        assert by_action.status_code == 200
        assert [row["request_id"] for row in by_action.json()["logs"]] == ["alpha-upscale"]

        by_status = client.get("/admin/api/logs?status=rejected&limit=100", auth=("admin", "admin123"))
        assert by_status.status_code == 200
        assert [row["request_id"] for row in by_status.json()["logs"]] == ["beta-rejected"]

        by_hour = client.get(
            "/admin/api/logs?created_from=2026-05-27T09:00&created_to=2026-05-27T10:00&limit=100",
            auth=("admin", "admin123"),
        )
        assert by_hour.status_code == 200
        assert [row["request_id"] for row in by_hour.json()["logs"]] == ["alpha-upscale"]

        combined = client.get(
            (
                f"/admin/api/logs?user_id={alpha_id}&action=generate&status=success"
                "&created_from=2026-05-27T08:00&created_to=2026-05-27T09:00&limit=100"
            ),
            auth=("admin", "admin123"),
        )
        assert combined.status_code == 200
        assert [row["request_id"] for row in combined.json()["logs"]] == ["alpha-success"]

        invalid_status = client.get("/admin/api/logs?status=unknown", auth=("admin", "admin123"))
        assert invalid_status.status_code == 400

        invalid_created_from = client.get(
            "/admin/api/logs?created_from=not-a-date",
            auth=("admin", "admin123"),
        )
        assert invalid_created_from.status_code == 400
        assert invalid_created_from.json()["message"] == "Invalid created_from filter"

        reversed_range = client.get(
            "/admin/api/logs?created_from=2026-05-27T10:00&created_to=2026-05-27T09:00",
            auth=("admin", "admin123"),
        )
        assert reversed_range.status_code == 400
        assert reversed_range.json()["message"] == "created_from must be earlier than created_to"


def test_admin_logs_api_keeps_filters_when_paginating_with_before_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        user_id = _create_admin_user(client, "filtered-pagination-user")
        for index in range(7):
            _insert_usage_log(
                client,
                user_id=user_id,
                request_id=f"filtered-page-{index}",
                action="generate" if index % 2 == 0 else "upscale",
                status="success",
                created_at=f"2026-05-27T00:00:0{index}+00:00",
            )

        first_page = client.get("/admin/api/logs?action=generate&limit=2", auth=("admin", "admin123"))
        assert first_page.status_code == 200
        first_body = first_page.json()
        assert [row["request_id"] for row in first_body["logs"]] == ["filtered-page-6", "filtered-page-4"]
        assert first_body["has_more"] is True

        _insert_usage_log(
            client,
            user_id=user_id,
            request_id="filtered-page-newer",
            action="generate",
            status="success",
            created_at="2026-05-27T00:01:00+00:00",
        )

        second_page = client.get(
            f"/admin/api/logs?action=generate&limit=2&before_id={first_body['next_before_id']}",
            auth=("admin", "admin123"),
        )
        assert second_page.status_code == 200
        second_body = second_page.json()
        assert [row["request_id"] for row in second_body["logs"]] == ["filtered-page-2", "filtered-page-0"]
        assert {row["action"] for row in second_body["logs"]} == {"generate"}
        assert second_body["has_more"] is False


def test_admin_logs_page_preserves_filter_controls(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        user_id = _create_admin_user(client, "render-filter-user")
        _insert_usage_log(
            client,
            user_id=user_id,
            request_id="render-filter-log",
            action="generate",
            status="success",
            created_at="2026-05-27T00:00:00+00:00",
        )

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        page = client.get(
            (
                f"/admin/logs?user_id={user_id}&created_from=2026-05-27T08:00"
                "&created_to=2026-05-27T09:00&action=generate&status=success&limit=25"
            )
        )
        assert page.status_code == 200
        assert f'id="logs-user-id-input" name="user_id" type="hidden" value="{user_id}"' in page.text
        assert 'id="logs-user-search-input" type="search" value="render-filter-user"' in page.text
        assert 'id="logs-created-from-input" name="created_from" type="hidden" value="2026-05-27T08:00"' in page.text
        assert 'id="logs-created-to-input" name="created_to" type="hidden" value="2026-05-27T09:00"' in page.text
        assert 'id="logs-time-range-toggle"' in page.text
        assert "2026-05-27 08:00 ~ 2026-05-27 09:00" in page.text
        assert 'id="logs-time-range-modal"' in page.text
        assert 'id="logs-calendar-days"' in page.text
        assert 'class="logs-calendar-weekdays"' in page.text
        assert 'id="logs-time-from-hour" aria-label="起始小时"' in page.text
        assert 'id="logs-time-to-hour" aria-label="截止小时"' in page.text
        assert 'id="logs-time-from-date" type="date"' not in page.text
        assert 'id="logs-time-to-date" type="date"' not in page.text
        assert "开始日期" not in page.text
        assert "结束日期" not in page.text
        assert "开始小时" not in page.text
        assert "结束小时" not in page.text
        assert 'data-range-preset="today"' in page.text
        assert '<option value="generate" selected>generate</option>' in page.text
        assert '<option value="success" selected>成功</option>' in page.text


def test_admin_users_search_api_supports_session_and_excludes_deleted_users(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        active_one = _create_admin_user(client, "alpha-search-one")
        active_two = _create_admin_user(client, "alpha-search-two")
        deleted = _create_admin_user(client, "alpha-search-deleted")
        _create_admin_user(client, "beta-search-user")
        delete_resp = client.delete(f"/admin/api/users/{deleted}", auth=("admin", "admin123"))
        assert delete_resp.status_code == 200

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        search = client.get("/admin/api/users/search?q=alpha-search&limit=5")
        assert search.status_code == 200
        users = search.json()["users"]
        assert {row["id"] for row in users} == {active_one, active_two}
        assert all(row["name"] != "alpha-search-deleted" for row in users)

        limited = client.get("/admin/api/users/search?q=alpha-search&limit=1")
        assert limited.status_code == 200
        assert len(limited.json()["users"]) == 1


def test_admin_users_search_api_treats_like_wildcards_as_literals(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        percent_id = _create_admin_user(client, "wild%literal")
        _create_admin_user(client, "wildXliteral")
        underscore_id = _create_admin_user(client, "under_score")
        _create_admin_user(client, "underXscore")
        backslash_id = _create_admin_user(client, "path\\literal")
        _create_admin_user(client, "pathliteral")

        percent = client.get(
            "/admin/api/users/search",
            params={"q": "wild%", "limit": "10"},
            auth=("admin", "admin123"),
        )
        assert percent.status_code == 200
        assert [row["id"] for row in percent.json()["users"]] == [percent_id]

        underscore = client.get(
            "/admin/api/users/search",
            params={"q": "under_", "limit": "10"},
            auth=("admin", "admin123"),
        )
        assert underscore.status_code == 200
        assert [row["id"] for row in underscore.json()["users"]] == [underscore_id]

        backslash = client.get(
            "/admin/api/users/search",
            params={"q": "path\\", "limit": "10"},
            auth=("admin", "admin123"),
        )
        assert backslash.status_code == 200
        assert [row["id"] for row in backslash.json()["users"]] == [backslash_id]


def _create_admin_user(client: TestClient, name: str) -> int:
    response = client.post(
        "/admin/api/users",
        auth=("admin", "admin123"),
        json={"name": name, "tier": "normal", "anlas_total": 100},
    )
    assert response.status_code == 200
    return int(response.json()["user_id"])


def _insert_usage_log(
    client: TestClient,
    *,
    user_id: int,
    request_id: str,
    action: str,
    status: str,
    created_at: str,
) -> None:
    client.app.state.db.execute(
        """
        INSERT INTO usage_logs (
            request_id, user_id, action, estimated_anlas_cost, status, log_level, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (request_id, user_id, action, 0, status, "INFO", created_at),
    )
