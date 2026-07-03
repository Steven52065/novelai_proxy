from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from helpers import write_test_config

AdminAuth = tuple[str, str]


@pytest.fixture
def admin_auth() -> AdminAuth:
    return ("admin", "admin123")


@pytest.fixture
def test_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_path = write_test_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    return config_path


@pytest.fixture
def app(test_config_path: Path):
    from app.main import app

    return app


@pytest.fixture
def client(app) -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def create_user(admin_auth: AdminAuth) -> Callable[..., int]:
    def _create_user(client: TestClient, name: str = "test-user", **overrides: Any) -> int:
        payload: dict[str, Any] = {
            "name": name,
            "tier": "normal",
            "anlas_total": 100,
            "reset_period": "month",
            "reset_day": 5,
        }
        payload.update(overrides)
        response = client.post("/admin/api/users", auth=admin_auth, json=payload)
        assert response.status_code == 200
        return int(response.json()["user_id"])

    return _create_user


def wait_until(predicate: Callable[[], bool], timeout: float = 3.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("timed out waiting for condition")


async def wait_until_async(
    predicate: Callable[[], bool],
    timeout: float = 3.0,
    interval: float = 0.01,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("timed out waiting for condition")


@pytest.fixture
def wait_for_condition() -> Callable[[Callable[[], bool], float, float], None]:
    return wait_until
