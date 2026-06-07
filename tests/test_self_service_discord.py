from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.database import Database, utc_now_iso
from helpers import write_test_config


class FakeDiscordClient:
    def __init__(self, *, user: dict | None = None, guilds: list[dict] | None = None, fail_at: str | None = None):
        self.user = user or {"id": "discord-1", "username": "tester", "global_name": "Tester", "avatar": "avatar"}
        self.guilds = guilds if guilds is not None else [{"id": "guild-1"}]
        self.fail_at = fail_at

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        return f"https://discord.example/oauth?state={state}&redirect_uri={redirect_uri}&scope=identify+guilds"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        if self.fail_at == "token":
            raise RuntimeError("token failed")
        return {"access_token": "secret-access-token", "refresh_token": "secret-refresh-token"}

    async def fetch_user(self, *, access_token: str) -> dict:
        if self.fail_at == "user":
            raise RuntimeError("user failed")
        return self.user

    async def fetch_guilds(self, *, access_token: str) -> list[dict]:
        if self.fail_at == "guilds":
            raise RuntimeError("guilds failed")
        return self.guilds


def test_discord_self_service_disabled_signup_returns_clear_status(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/signup")

        assert resp.status_code == 404
        assert resp.json()["message"] == "Discord self-service is disabled"


def test_discord_oauth_state_mismatch_rejects_without_creating_user(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        start = client.get("/auth/discord/start", follow_redirects=False)
        assert start.status_code == 303
        bad = client.get("/auth/discord/callback?code=ok&state=bad")

        assert bad.status_code == 400
        assert bad.json()["message"] == "Invalid Discord OAuth state"
        assert _count_rows(client.app.state.db, "users") == 0


def test_discord_oauth_failures_do_not_create_user(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(fail_at="token")
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 502
        assert _count_rows(client.app.state.db, "users") == 0


def test_discord_user_outside_required_guild_is_rejected(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(guilds=[{"id": "other-guild"}])
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 403
        assert resp.json()["message"] == "Discord user is not in the required guild"
        assert _count_rows(client.app.state.db, "users") == 0


def test_discord_signup_creates_group_user_and_shows_api_key_once(tmp_path: Path, monkeypatch):
    config_path, group_id = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)

        assert page.status_code == 200
        api_key = _extract_api_key(page.text)
        assert "Discord: Tester" in page.text
        assert client.get("/account").text.find("nai_proxy_") == -1

        user = client.app.state.db.query_one("SELECT id, name, group_id, api_key FROM users")
        assert user["name"] == "Discord: Tester"
        assert user["group_id"] == group_id
        assert user["api_key"] is None
        quota = client.app.state.db.query_one("SELECT total, reset_period, reset_day FROM user_anlas_quota WHERE user_id = ?", (user["id"],))
        assert dict(quota) == {"total": 42, "reset_period": "week", "reset_day": 2}
        link = client.app.state.db.query_one("SELECT discord_user_id, discord_username, discord_global_name FROM discord_user_links")
        assert dict(link) == {
            "discord_user_id": "discord-1",
            "discord_username": "tester",
            "discord_global_name": "Tester",
        }

        sub = client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"})
        assert sub.status_code == 200


def test_discord_repeat_login_syncs_names_without_duplicate_or_overwriting_manual_name(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]

        _complete_discord_login(
            client,
            user={"id": "discord-1", "username": "tester2", "global_name": "Tester Two", "avatar": "avatar-2"},
        )
        assert _count_rows(client.app.state.db, "users") == 1
        user = client.app.state.db.query_one("SELECT name FROM users WHERE id = ?", (user_id,))
        assert user["name"] == "Discord: Tester Two"
        link = client.app.state.db.query_one("SELECT discord_username, discord_global_name, discord_avatar FROM discord_user_links")
        assert dict(link) == {
            "discord_username": "tester2",
            "discord_global_name": "Tester Two",
            "discord_avatar": "avatar-2",
        }

        client.app.state.db.execute("UPDATE users SET name = 'Manual Name' WHERE id = ?", (user_id,))
        _complete_discord_login(
            client,
            user={"id": "discord-1", "username": "tester3", "global_name": "Tester Three", "avatar": "avatar-3"},
        )
        user = client.app.state.db.query_one("SELECT name FROM users WHERE id = ?", (user_id,))
        assert user["name"] == "Manual Name"
        link = client.app.state.db.query_one("SELECT discord_username, discord_global_name FROM discord_user_links")
        assert dict(link) == {"discord_username": "tester3", "discord_global_name": "Tester Three"}


def test_discord_linked_soft_deleted_user_is_rejected(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]
        client.app.state.db.execute("UPDATE users SET deleted_at = ? WHERE id = ?", (utc_now_iso(), user_id))

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient()
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 403
        assert resp.json()["message"] == "Account was deleted; contact administrator"


def test_self_service_reset_key_invalidates_old_key_and_does_not_store_discord_tokens(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        old_key = _extract_api_key(page.text)

        reset = client.post("/account/reset-key", follow_redirects=True)
        assert reset.status_code == 200
        new_key = _extract_api_key(reset.text)
        assert new_key != old_key
        assert client.get("/user/subscription", headers={"Authorization": f"Bearer {old_key}"}).status_code == 401
        assert client.get("/user/subscription", headers={"Authorization": f"Bearer {new_key}"}).status_code == 200

        columns = {row["name"] for row in client.app.state.db.query_all("PRAGMA table_info(discord_user_links)")}
        assert "access_token" not in columns
        assert "refresh_token" not in columns
        link_rows = client.app.state.db.query_all("SELECT * FROM discord_user_links")
        assert "secret-access-token" not in str([tuple(row) for row in link_rows])
        assert "secret-refresh-token" not in str([tuple(row) for row in link_rows])


def _write_self_service_config(tmp_path: Path) -> tuple[Path, int]:
    config_path = write_test_config(tmp_path)
    db = Database(str(tmp_path / "test.db"))
    db.init_schema()
    group_id = _create_default_group(db)
    db.close()
    with config_path.open("a", encoding="utf-8") as f:
        f.write(
            f"""
self_service:
  discord:
    enabled: true
    client_id: "client-id"
    client_secret: "client-secret"
    redirect_uri: "http://testserver/auth/discord/callback"
    required_guild_id: "guild-1"
    default_group_id: {group_id}
    session_secret: "test-session-secret"
"""
        )
    return config_path, group_id


def _create_default_group(db: Database) -> int:
    cursor = db.execute(
        """
        INSERT INTO user_groups (
            name, is_active, default_tier, default_free_small_only,
            default_allowed_endpoints, default_anlas_total,
            default_reset_period, default_reset_day, created_at
        )
        VALUES ('discord-default', 1, 'vip', 0, 'generate-image', 42, 'week', 2, ?)
        """,
        (utc_now_iso(),),
    )
    return int(cursor.lastrowid)


def _complete_discord_login(client: TestClient, user: dict | None = None):
    state = _start_state(client)
    client.app.state.discord_oauth_client = FakeDiscordClient(user=user)
    return client.get(f"/auth/discord/callback?code=ok&state={state}", follow_redirects=True)


def _start_state(client: TestClient) -> str:
    start = client.get("/auth/discord/start", follow_redirects=False)
    assert start.status_code == 303
    query = parse_qs(urlparse(start.headers["location"]).query)
    return query["state"][0]


def _extract_api_key(text: str) -> str:
    match = re.search(r"nai_proxy_[A-Za-z0-9_-]+", text)
    assert match is not None
    return match.group(0)


def _count_rows(db: Database, table: str) -> int:
    return int(db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"])
