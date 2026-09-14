from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from app.api_errors import APIError

from app.config import UpstreamAutoDisableConfig
from app.upstream import _raise_response_error
from app.upstream_auto_disable import UpstreamAutoDisableService
from helpers import PAYLOAD, FakeUpstream, write_test_config, write_test_config_with_upstreams


class Always403Upstream(FakeUpstream):
    async def generate_image_payload_zip(self, payload):
        self.generate_started_at.append(time.monotonic())
        self.last_generate_payload = payload
        raise APIError(
            "Forbidden",
            request=payload,
            response={"message": "Forbidden"},
            code="403",
        )


class FakeRuntime:
    def __init__(self):
        self.disabled = []

    def disable_upstream(self, upstream_id):
        self.disabled.append(upstream_id)
        return object()


class FakeNotifications:
    def __init__(self):
        self.created = []

    def create(self, **kwargs):
        self.created.append(kwargs)


def test_auto_disable_ignores_disabled_config_and_unmatched_rules():
    for config in (
        UpstreamAutoDisableConfig(enabled=False, status_codes=[403]),
        UpstreamAutoDisableConfig(enabled=True, status_codes=[500]),
        UpstreamAutoDisableConfig(enabled=True, status_codes=[], error_types=["OutOfMemory"]),
    ):
        runtime = FakeRuntime()
        notifications = FakeNotifications()
        service = UpstreamAutoDisableService(
            config=config,
            runtime=runtime,
            notifications=notifications,
        )

        service.handle_api_error(
            "opus-a",
            APIError("Forbidden", request={}, response={"message": "Forbidden"}, code="403"),
        )

        assert runtime.disabled == []
        assert notifications.created == []


def test_auto_disable_matches_status_code_or_error_type():
    cases = (
        (
            APIError("Server error", request={}, response={"errorType": "OutOfMemory"}, code="503"),
            UpstreamAutoDisableConfig(status_codes=[], error_types=["OutOfMemory"]),
            "OutOfMemory",
        ),
        (
            APIError("Forbidden", request={}, response={"type": "Unexpected"}, code="403"),
            UpstreamAutoDisableConfig(status_codes=[403], error_types=[]),
            "403",
        ),
    )

    for error, config, expected_content in cases:
        runtime = FakeRuntime()
        notifications = FakeNotifications()
        service = UpstreamAutoDisableService(
            config=config,
            runtime=runtime,
            notifications=notifications,
        )

        service.handle_api_error("opus-a", error)

        assert runtime.disabled == ["opus-a"]
        assert len(notifications.created) == 1
        assert expected_content in notifications.created[0]["content"]
        assert notifications.created[0]["metadata"]["upstream_id"] == "opus-a"


@pytest.mark.parametrize(
    ("http_status", "status_codes", "error_fields", "should_disable", "expected_error_type"),
    [
        pytest.param(400, None, {}, False, "APIError", id="default-rules"),
        pytest.param(400, [403], {}, False, "APIError", id="legacy-status-list-excludes-400"),
        pytest.param(400, [], {}, False, "APIError", id="empty-status-list"),
        pytest.param(400, [400], {}, True, "APIError", id="explicit-400-rule"),
        pytest.param(400, [], {"type": "AuthError"}, True, "AuthError", id="400-type-match-only"),
        pytest.param(500, [], {"name": "AuthError"}, True, "AuthError", id="500-type-match-only"),
        pytest.param(401, [], {}, False, "APIError", id="401-without-type-is-not-auth-error"),
        pytest.param(402, [], {}, False, "APIError", id="402-without-type-is-not-auth-error"),
        pytest.param(401, [], {"type": "ValidationError"}, False, "ValidationError", id="neither-matches"),
        pytest.param(401, [401], {"errorType": "ValidationError"}, True, "ValidationError", id="status-match-only"),
        pytest.param(403, [403], {"type": "AuthError"}, True, "AuthError", id="both-match"),
        pytest.param(401, None, {}, True, "APIError", id="default-401-status-match"),
    ],
)
def test_upstream_status_and_error_type_disable_rules_are_independent(
    tmp_path: Path, monkeypatch, http_status, status_codes, error_fields, should_disable, expected_error_type
):
    config_path = write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"])
    # None 使用默认配置；其余模拟升级前未配置 error_types 的状态码规则。
    if status_codes is not None:
        with config_path.open("a", encoding="utf-8") as config_file:
            config_file.write(f"\nupstream_auto_disable:\n  status_codes: {status_codes}\n")
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    response = SimpleNamespace(
        status_code=http_status,
        content=b"",
        json=lambda: {"message": "Upstream rejected this request", **error_fields},
    )

    class InvalidInputUpstream(FakeUpstream):
        async def generate_image_payload_zip(self, payload):
            if not isinstance(payload.get("input"), str):
                # 必须经过真实传输层的错误映射，手工构造 APIError 会漏掉本次回归。
                _raise_response_error(response, payload)
            return await super().generate_image_payload_zip(payload)

    with TestClient(app) as client:
        upstream = InvalidInputUpstream()
        app.state.upstream = upstream
        app.state.upstream_clients["opus-b"] = upstream
        user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "validation-error-user", "tier": "normal", "anlas_total": 100},
        ).json()
        headers = {"Authorization": f"Bearer {user['api_key']}"}
        invalid_payload = PAYLOAD | {
            "input": 123,
            "parameters": PAYLOAD["parameters"] | {"steps": 30},
        }

        for _ in range(2):
            failed = client.post("/ai/generate-image", headers=headers, json=invalid_payload)
            assert failed.status_code == http_status
            quota = app.state.quota_manager.get_snapshot(user["user_id"])
            assert quota.used == 0
            assert quota.reserved == 0

        failures = app.state.db.query_all(
            "SELECT status, estimated_anlas_cost FROM usage_logs WHERE user_id = ?",
            (user["user_id"],),
        )
        assert len(failures) == 2
        assert all(row["status"] == "failed" and row["estimated_anlas_cost"] > 0 for row in failures)
        expected_enabled = 0 if should_disable else 2
        assert len(app.state.upstream_clients) == expected_enabled
        assert len(app.state.upstream_runtime.repository.list(include_disabled=False)) == expected_enabled
        notifications = app.state.admin_notifications.pending()
        assert len(notifications) == (2 if should_disable else 0)
        for notification in notifications:
            assert notification.metadata["status_code"] == http_status
            assert notification.metadata["error_type"] == expected_error_type

        generated = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        assert generated.status_code == (503 if should_disable else 201)


def test_upstream_403_auto_disables_channel_and_creates_notification(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "NOVELAI_PROXY_CONFIG",
        str(write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"])),
    )
    from app.main import app

    with TestClient(app) as client:
        failing = Always403Upstream()
        fallback = FakeUpstream()
        app.state.upstream = failing
        app.state.upstream_clients["opus-b"] = fallback

        user = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "auto-disable-user", "tier": "normal", "anlas_total": 100},
        ).json()
        headers = {"Authorization": f"Bearer {user['api_key']}"}

        failed = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)

        assert failed.status_code == 403
        assert len(failing.generate_started_at) == 1
        upstreams = client.get("/admin/api/upstreams", auth=("admin", "admin123")).json()["upstreams"]
        opus_a = next(row for row in upstreams if row["id"] == "opus-a")
        assert opus_a["enabled"] is False
        assert "opus-a" not in app.state.upstream_clients

        pending = client.get("/admin/api/notifications/pending", auth=("admin", "admin123")).json()["notifications"]
        assert len(pending) == 1
        assert pending[0]["event_type"] == "upstream_auto_disabled"
        assert "opus-a" in pending[0]["content"]
        assert "403" in pending[0]["content"]
        assert "pst-test-token" not in str(pending[0])

        generated = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)

        assert generated.status_code == 201
        assert len(fallback.generate_started_at) == 1


def test_auto_disable_notification_dedupes_until_upstream_is_reenabled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "NOVELAI_PROXY_CONFIG",
        str(write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"])),
    )
    from app.main import app

    with TestClient(app) as client:
        error = APIError("Forbidden", request={}, response={"message": "Forbidden"}, code="403")

        app.state.upstream_auto_disable.handle_api_error("opus-a", error)
        app.state.upstream_auto_disable.handle_api_error("opus-a", error)

        pending = client.get("/admin/api/notifications/pending", auth=("admin", "admin123")).json()["notifications"]
        assert len(pending) == 1

        enabled = client.patch(
            "/admin/api/upstreams/opus-a",
            auth=("admin", "admin123"),
            json={"enabled": True},
        )
        assert enabled.status_code == 200

        app.state.upstream_auto_disable.handle_api_error("opus-a", error)

        pending = client.get("/admin/api/notifications/pending", auth=("admin", "admin123")).json()["notifications"]
        assert len(pending) == 2


def test_pending_upstream_ids_returns_only_matching_event_type(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        repo = app.state.admin_notifications
        repo.create(
            event_type="upstream_auto_disabled",
            title="a",
            content="a",
            metadata={"upstream_id": "opus-a"},
        )
        repo.create(
            event_type="upstream_auto_disabled",
            title="b",
            content="b",
            metadata={"upstream_id": "opus-b"},
        )
        repo.create(
            event_type="upstream_auto_disabled",
            title="empty-id",
            content="empty-id",
            metadata={"upstream_id": ""},
        )
        repo.create(
            event_type="other",
            title="other",
            content="other",
            metadata={"upstream_id": "opus-other"},
        )

        assert repo.pending_upstream_ids("upstream_auto_disabled") == {"opus-a", "opus-b"}


def test_pending_upstream_ids_skips_dismissed_and_malformed_metadata(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        repo = app.state.admin_notifications
        dismissed = repo.create(
            event_type="upstream_auto_disabled",
            title="dismissed",
            content="dismissed",
            metadata={"upstream_id": "opus-dismissed"},
        )
        repo.dismiss(dismissed.id)
        malformed = repo.create(
            event_type="upstream_auto_disabled",
            title="malformed",
            content="malformed",
            metadata={"upstream_id": "opus-malformed"},
        )
        client.app.state.db.execute(
            "UPDATE admin_notifications SET metadata = ? WHERE id = ?",
            ("not-json", malformed.id),
        )
        list_metadata = repo.create(
            event_type="upstream_auto_disabled",
            title="list",
            content="list",
            metadata={"upstream_id": "opus-list"},
        )
        client.app.state.db.execute(
            "UPDATE admin_notifications SET metadata = ? WHERE id = ?",
            ('["not", "a", "dict"]', list_metadata.id),
        )
        repo.create(
            event_type="upstream_auto_disabled",
            title="empty",
            content="empty",
            metadata={},
        )

        assert repo.pending_upstream_ids("upstream_auto_disabled") == set()


def test_admin_notifications_pending_order_and_dismiss(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        repo = app.state.admin_notifications
        later = repo.create(
            event_type="test",
            title="Later",
            content="later event",
            event_time="2026-01-02T00:00:00+00:00",
        )
        earlier = repo.create(
            event_type="test",
            title="Earlier",
            content="earlier event",
            event_time="2026-01-01T00:00:00+00:00",
        )

        pending = client.get("/admin/api/notifications/pending", auth=("admin", "admin123"))

        assert pending.status_code == 200
        rows = pending.json()["notifications"]
        assert [row["id"] for row in rows] == [earlier.id, later.id]
        assert rows[0]["event_time_display"] == "2026-01-01 08:00:00 UTC+8"

        dismissed = client.post(
            f"/admin/api/notifications/{earlier.id}/dismiss",
            auth=("admin", "admin123"),
        )
        assert dismissed.status_code == 200

        pending_after = client.get("/admin/api/notifications/pending", auth=("admin", "admin123")).json()["notifications"]
        assert [row["id"] for row in pending_after] == [later.id]
