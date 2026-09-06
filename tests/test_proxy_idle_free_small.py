
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from helpers import PAYLOAD, FakeUpstream, write_test_config


def test_idle_free_small_fallback_succeeds_after_daily_allowance_is_used(tmp_path: Path, monkeypatch):
    config_path = _write_idle_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        user = _create_user(
            client,
            free_small_daily_limit_enabled=True,
            free_small_daily_limit=1,
            idle_free_small_multiplier=1,
        )
        headers = {"Authorization": f"Bearer {user['api_key']}"}

        first = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        second = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)

        assert first.status_code == 201
        assert second.status_code == 201

        normal = client.app.state.free_small_daily_limit_manager.get_snapshot(user["user_id"])
        idle = client.app.state.idle_free_small_daily_limit_manager.get_snapshot(user["user_id"])
        assert normal.used == 1
        assert normal.reserved == 0
        assert idle.used == 1
        assert idle.reserved == 0
        assert idle.enabled is True
        assert idle.limit == 1


def test_idle_free_small_requires_certainly_free_generate_request(tmp_path: Path, monkeypatch):
    config_path = _write_idle_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        user = _create_user(
            client,
            free_small_daily_limit_enabled=True,
            free_small_daily_limit=1,
            idle_free_small_multiplier=1,
        )
        headers = {"Authorization": f"Bearer {user['api_key']}"}

        first = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        unknown = PAYLOAD | {"parameters": PAYLOAD["parameters"] | {"zzz_unknown": 1}}
        second = client.post("/ai/generate-image", headers=headers, json=unknown)

        assert first.status_code == 201
        assert second.status_code == 429
        body = second.json()
        assert body["limit"] == 1
        assert body["used"] == 1
        idle = client.app.state.idle_free_small_daily_limit_manager.get_snapshot(user["user_id"])
        assert idle.used == 0
        assert idle.reserved == 0


def _write_idle_config(tmp_path: Path) -> Path:
    config_path = write_test_config(tmp_path)
    with config_path.open("a", encoding="utf-8") as f:
        f.write(
            "\nidle_free_small:\n"
            "  occupancy_threshold_percent: 50\n"
            "  min_idle_seconds: 0\n"
        )
    return config_path


def _create_user(client: TestClient, **overrides):
    payload = {
        "name": "idle-proxy-user",
        "tier": "normal",
        "anlas_total": 100,
    }
    payload.update(overrides)
    response = client.post("/admin/api/users", auth=("admin", "admin123"), json=payload)
    assert response.status_code == 200
    return response.json()
