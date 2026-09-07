
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlite3 import OperationalError

import pytest
from fastapi.testclient import TestClient

from helpers import PAYLOAD, BlockingFakeUpstream, FakeUpstream, write_test_config


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


def test_normal_daily_pool_is_used_before_idle_fallback(tmp_path: Path, monkeypatch):
    config_path = _write_idle_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        user = _create_user(
            client,
            free_small_daily_limit_enabled=True,
            free_small_daily_limit=2,
            idle_free_small_multiplier=1,
        )
        api_key = user["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        assert client.post("/ai/generate-image", headers=headers, json=PAYLOAD).status_code == 201
        assert client.post("/ai/generate-image", headers=headers, json=PAYLOAD).status_code == 201
        assert client.post("/ai/generate-image", headers=headers, json=PAYLOAD).status_code == 201

        normal = client.app.state.free_small_daily_limit_manager.get_snapshot(user["user_id"])
        idle = client.app.state.idle_free_small_daily_limit_manager.get_snapshot(user["user_id"])
        assert normal.used == 2
        assert idle.used == 1


def test_idle_fallback_rejected_while_upstream_is_busy(tmp_path: Path, monkeypatch):
    config_path = _write_idle_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    release_event = threading.Event()
    with TestClient(app) as client:
        app.state.upstream = BlockingFakeUpstream(release_event)
        user = _create_user(
            client,
            free_small_daily_limit_enabled=True,
            free_small_daily_limit=1,
            idle_free_small_multiplier=1,
        )
        api_key = user["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(client.post, "/ai/generate-image", headers=headers, json=PAYLOAD)
            deadline = time.monotonic() + 5
            while not app.state.upstream.generate_started_at and time.monotonic() < deadline:
                time.sleep(0.01)

            second = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
            assert second.status_code == 429
            assert second.json()["used"] == 0
            assert second.json()["reserved"] == 1

            release_event.set()
            assert first.result(timeout=5).status_code == 201

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


def test_idle_fallback_triggers_when_normal_reserved_plus_used_exhausted(tmp_path: Path, monkeypatch):
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
        client.app.state.free_small_daily_limit_manager.reserve(user["user_id"], 1)
        api_key = user["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        response = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        assert response.status_code == 201
        normal = client.app.state.free_small_daily_limit_manager.get_snapshot(user["user_id"])
        idle = client.app.state.idle_free_small_daily_limit_manager.get_snapshot(user["user_id"])
        assert normal.used == 0
        assert normal.reserved == 1
        assert idle.used == 1
        assert idle.reserved == 0


def test_idle_fallback_disabled_when_multiplier_zero(tmp_path: Path, monkeypatch):
    config_path = _write_idle_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        user = _create_user(
            client,
            free_small_daily_limit_enabled=True,
            free_small_daily_limit=1,
            idle_free_small_multiplier=0,
        )
        api_key = user["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        assert client.post("/ai/generate-image", headers=headers, json=PAYLOAD).status_code == 201
        second = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        assert second.status_code == 429
        idle = client.app.state.idle_free_small_daily_limit_manager.get_snapshot(user["user_id"])
        assert idle.enabled is False
        assert idle.used == 0
        assert idle.reserved == 0


@pytest.mark.parametrize("use_idle_pool", [False, True])
def test_quota_read_error_releases_daily_reservation_before_queue(tmp_path: Path, monkeypatch, use_idle_pool):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(_write_idle_config(tmp_path)))
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        upstream = FakeUpstream()
        app.state.upstream = upstream
        user = _create_user(
            client,
            free_small_daily_limit_enabled=True,
            free_small_daily_limit=1,
            idle_free_small_multiplier=1,
        )
        user_id = user["user_id"]
        normal = app.state.free_small_daily_limit_manager
        idle = app.state.idle_free_small_daily_limit_manager
        if use_idle_pool:
            normal.confirm(normal.reserve(user_id, 1))

        def fail_quota_read(_user_id, _cost):
            raise OperationalError("quota read failed")

        headers = {"Authorization": f"Bearer {user['api_key']}"}
        with monkeypatch.context() as patch:
            patch.setattr(app.state.quota_manager, "reserve", fail_quota_read)
            response = client.post("/ai/generate-image", headers=headers, json=PAYLOAD)
        assert response.status_code == 500
        assert not upstream.generate_started_at
        assert normal.get_snapshot(user_id).reserved == 0
        assert idle.get_snapshot(user_id).reserved == 0
        assert idle.get_snapshot(user_id).used == 0
        assert app.state.db.query_one("SELECT COUNT(*) AS count FROM usage_logs WHERE user_id = ?", (user_id,))["count"] == 0

        assert client.post("/ai/generate-image", headers=headers, json=PAYLOAD).status_code == 201
        assert normal.get_snapshot(user_id).used == 1
        assert idle.get_snapshot(user_id).used == int(use_idle_pool)
        assert idle.get_snapshot(user_id).reserved == 0
