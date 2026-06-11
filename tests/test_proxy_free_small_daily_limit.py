from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from helpers import PAYLOAD, FakeUpstream, write_test_config, write_test_config_with_upstreams
from proxy_route_fakes import Always429Upstream, NeverReturningUpstream


def test_user_free_small_daily_limit_returns_429_after_daily_allowance(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        user = _create_user(client, free_small_daily_limit_enabled=True, free_small_daily_limit=1)
        headers = {"Authorization": f"Bearer {user['api_key']}"}

        first = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        second = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)

        assert first.status_code == 201
        assert second.status_code == 429
        assert second.headers["retry-after"] != ""
        body = second.json()
        assert body["limit_scope"] == "user"
        assert body["limit"] == 1
        assert body["used"] == 1
        assert body["reserved"] == 0
        assert body["requested"] == 1
        assert body["remaining"] == 0

        daily = client.get("/user/subscription", headers=headers).json()["proxyFreeSmallDailyLimit"]
        assert daily["enabled"] is True
        assert daily["scope"] == "user"
        assert daily["used"] == 1
        assert daily["reserved"] == 0
        assert daily["available"] == 0

        logs = client.get("/admin/api/logs", auth=("admin", "admin123")).json()["logs"]
        rejected_log = next(row for row in logs if row["status"] == "rejected")
        assert rejected_log["error_code"] == "free_small_daily_limit_exceeded"


def test_group_daily_limit_default_is_copied_per_user_and_independent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        group_id = _create_group(client, free_small_daily_limit_enabled=True, free_small_daily_limit=1)
        first_user = _create_user(client, group_id=group_id)
        second_user = _create_user(client, group_id=group_id)

        first_headers = {"Authorization": f"Bearer {first_user['api_key']}"}
        second_headers = {"Authorization": f"Bearer {second_user['api_key']}"}

        assert client.post("/ai/generate-image", headers=first_headers, json=PAYLOAD).status_code == 201
        assert client.post("/ai/generate-image", headers=second_headers, json=PAYLOAD).status_code == 201
        exceeded = client.post("/ai/generate-image", headers=first_headers, json=PAYLOAD)

        assert exceeded.status_code == 429
        assert exceeded.json()["limit_scope"] == "user"
        assert exceeded.json()["limit"] == 1


def test_user_level_limit_overrides_group_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        group_id = _create_group(client, free_small_daily_limit_enabled=True, free_small_daily_limit=1)
        user = _create_user(
            client,
            group_id=group_id,
            free_small_daily_limit_enabled=True,
            free_small_daily_limit=2,
        )
        headers = {"Authorization": f"Bearer {user['api_key']}"}

        assert client.post("/ai/generate-image", headers=headers, json=PAYLOAD).status_code == 201
        assert client.post("/ai/generate-image", headers=headers, json=PAYLOAD).status_code == 201
        exceeded = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)

        assert exceeded.status_code == 429
        assert exceeded.json()["limit_scope"] == "user"
        assert exceeded.json()["limit"] == 2


def test_free_small_daily_reservation_is_released_on_timeout(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "NOVELAI_PROXY_CONFIG",
        str(write_test_config(tmp_path, upstream_execution_timeout_seconds=0.02)),
    )
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = NeverReturningUpstream()
        user = _create_user(client, free_small_daily_limit_enabled=True, free_small_daily_limit=1)
        headers = {"Authorization": f"Bearer {user['api_key']}"}

        resp = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)

        assert resp.status_code == 504
        daily = client.get("/user/subscription", headers=headers).json()["proxyFreeSmallDailyLimit"]
        assert daily["used"] == 0
        assert daily["reserved"] == 0
        assert daily["available"] == 1


def test_429_retry_success_confirms_daily_reservation_once(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config_with_upstreams(tmp_path, ["opus-a", "opus-b"])))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = Always429Upstream()
        app.state.upstream_clients["opus-b"] = FakeUpstream()
        user = _create_user(client, free_small_daily_limit_enabled=True, free_small_daily_limit=1)
        headers = {"Authorization": f"Bearer {user['api_key']}"}

        resp = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)

        assert resp.status_code == 201
        daily = client.get("/user/subscription", headers=headers).json()["proxyFreeSmallDailyLimit"]
        assert daily["used"] == 1
        assert daily["reserved"] == 0


def test_429_retry_exhaustion_releases_daily_reservation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "NOVELAI_PROXY_CONFIG",
        str(write_test_config_with_upstreams(tmp_path, ["opus-a"], retry_429_max_attempts=3)),
    )
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = Always429Upstream()
        user = _create_user(client, free_small_daily_limit_enabled=True, free_small_daily_limit=1)
        headers = {"Authorization": f"Bearer {user['api_key']}"}

        resp = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)

        assert resp.status_code == 429
        daily = client.get("/user/subscription", headers=headers).json()["proxyFreeSmallDailyLimit"]
        assert daily["used"] == 0
        assert daily["reserved"] == 0
        assert daily["available"] == 1


def test_paid_or_uncertain_generation_does_not_consume_free_small_daily_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        user = _create_user(client, free_small_daily_limit_enabled=True, free_small_daily_limit=1)
        headers = {"Authorization": f"Bearer {user['api_key']}"}
        paid_payload = PAYLOAD | {"parameters": PAYLOAD["parameters"] | {"steps": 29}}

        first = client.post("/ai/generate-image", headers=headers, json=paid_payload)
        second = client.post("/ai/generate-image", headers=headers, json=paid_payload)

        assert first.status_code == 201
        assert second.status_code == 201
        daily = client.get("/user/subscription", headers=headers).json()["proxyFreeSmallDailyLimit"]
        assert daily["used"] == 0
        assert daily["reserved"] == 0
        assert daily["available"] == 1


def _create_user(client: TestClient, **overrides):
    payload = {
        "name": "daily-user",
        "tier": "normal",
        "anlas_total": 100,
    }
    payload.update(overrides)
    response = client.post("/admin/api/users", auth=("admin", "admin123"), json=payload)
    assert response.status_code == 200
    return response.json()


def _create_group(client: TestClient, **overrides) -> int:
    payload = {
        "name": "daily-group",
        "is_active": True,
        "default_tier": "normal",
        "default_free_small_only": False,
        "default_allowed_endpoints": ["generate-image"],
        "default_allowed_upstreams": [],
        "default_anlas_total": 100,
        "default_reset_period": "month",
        "default_reset_day": 1,
    }
    payload.update(overrides)
    response = client.post("/admin/api/user-groups", auth=("admin", "admin123"), json=payload)
    assert response.status_code == 200
    return response.json()["group_id"]
