from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

import app.self_service.accounts as accounts
from app.database import Database, utc_now_iso
from app.quota_manager import QuotaManager
from app.self_service.routes import API_KEY_FLASH_COOKIE
from helpers import PAYLOAD, FakeUpstream, csrf_form, write_test_config


DISCORD_USER_ID = "100000000000000001"
SECOND_DISCORD_USER_ID = "100000000000000002"
REQUIRED_GUILD_ID = "200000000000000001"
OTHER_GUILD_ID = "200000000000000002"


class FakeDiscordClient:
    def __init__(self, *, user: object | None = None, guilds: object | None = None, fail_at: str | None = None):
        self.user = (
            user
            if user is not None
            else {"id": DISCORD_USER_ID, "username": "tester", "global_name": "Tester", "avatar": "avatar"}
        )
        self.guilds = guilds if guilds is not None else [{"id": REQUIRED_GUILD_ID}]
        self.fail_at = fail_at

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        return f"https://discord.example/oauth?state={state}&redirect_uri={redirect_uri}&scope=identify+guilds"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        if self.fail_at == "token":
            raise RuntimeError("token failed")
        return {"access_token": "secret-access-token", "refresh_token": "secret-refresh-token"}

    async def fetch_user(self, *, access_token: str) -> object:
        if self.fail_at == "user":
            raise RuntimeError("user failed")
        return self.user

    async def fetch_guilds(self, *, access_token: str) -> object:
        if self.fail_at == "guilds":
            raise RuntimeError("guilds failed")
        return self.guilds


class HTTPStatusErrorDiscordClient(FakeDiscordClient):
    async def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        request = httpx.Request(
            "POST",
            "https://discord.com/api/oauth2/token?code=secret-code&state=secret-state",
        )
        response = httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "bad authorization code",
                "access_token": "secret-access-token",
                "refresh_token": "secret-refresh-token",
            },
            request=request,
        )
        raise httpx.HTTPStatusError(
            "Client error '400 Bad Request' for url 'https://discord.com/api/oauth2/token?code=secret-code'",
            request=request,
            response=response,
        )


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


def test_discord_oauth_failure_debug_log_includes_phase_and_redacts_secrets(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        state = _start_state(client)
        client.app.state.discord_oauth_client = HTTPStatusErrorDiscordClient()
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 502

    messages = (tmp_path / "logs" / "novelai_proxy.log").read_text(encoding="utf-8")
    assert "discord oauth failure details=" in messages
    assert '"phase": "exchange_code"' in messages
    assert '"status_code": 400' in messages
    assert "invalid_grant" in messages
    assert "bad authorization code" in messages
    assert "secret-access-token" not in messages
    assert "secret-refresh-token" not in messages
    assert "secret-code" not in messages
    assert "secret-state" not in messages


def test_discord_user_outside_required_guild_is_rejected(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(guilds=[{"id": OTHER_GUILD_ID}])
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 403
        assert resp.json()["message"] == "Discord user is not in the required guild"
        assert _count_rows(client.app.state.db, "users") == 0


def test_discord_login_outside_required_guild_disables_existing_account(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        api_key = _extract_api_key(page.text)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(guilds=[{"id": OTHER_GUILD_ID}])
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 403
        assert resp.json()["message"] == "Discord user is not in the required guild"
        user = client.app.state.db.query_one("SELECT is_active FROM users WHERE id = ?", (user_id,))
        assert user["is_active"] == 0
        api_resp = client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"})
        assert api_resp.status_code == 403
        assert api_resp.json()["message"] == "Account disabled"
        assert client.get("/account").status_code == 403


@pytest.mark.parametrize(
    "guilds",
    [
        {"id": REQUIRED_GUILD_ID},
        [None],
        [{"name": "missing-id"}],
        [{"id": 200000000000000001}],
        [{"id": True}],
        [{"id": {"unexpected": True}}],
        [{"id": [REQUIRED_GUILD_ID]}],
        [{"id": "not-a-snowflake"}],
        [{"id": "0200000000000000001"}],
        [{"id": str(1 << 64)}],
        [{"id": REQUIRED_GUILD_ID}, {"id": {"unexpected": True}}],
    ],
)
def test_invalid_discord_guilds_response_does_not_disable_existing_account(
    tmp_path: Path,
    monkeypatch,
    guilds: object,
):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        api_key = _extract_api_key(page.text)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(guilds=guilds)
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 502
        assert resp.json()["message"] == "Discord OAuth request failed"
        user = client.app.state.db.query_one("SELECT is_active FROM users WHERE id = ?", (user_id,))
        assert user["is_active"] == 1
        assert client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"}).status_code == 200


@pytest.mark.parametrize(
    "user",
    [
        [],
        {"username": "missing-id"},
        {"id": 100000000000000001, "username": "tester"},
        {"id": True, "username": "tester"},
        {"id": {"unexpected": True}, "username": "tester"},
        {"id": "not-a-snowflake", "username": "tester"},
        {"id": "0100000000000000001", "username": "tester"},
        {"id": str(1 << 64), "username": "tester"},
        {"id": DISCORD_USER_ID},
        {"id": DISCORD_USER_ID, "username": {"unexpected": True}},
        {"id": DISCORD_USER_ID, "username": "tester", "global_name": []},
        {"id": DISCORD_USER_ID, "username": "tester", "avatar": False},
    ],
)
def test_invalid_discord_user_response_does_not_disable_existing_account(
    tmp_path: Path,
    monkeypatch,
    user: object,
):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        api_key = _extract_api_key(page.text)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(user=user)
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 502
        assert resp.json()["message"] == "Discord OAuth request failed"
        existing_user = client.app.state.db.query_one("SELECT is_active FROM users WHERE id = ?", (user_id,))
        assert existing_user["is_active"] == 1
        assert client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"}).status_code == 200


def test_discord_guilds_request_failure_does_not_disable_existing_account(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        api_key = _extract_api_key(page.text)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(fail_at="guilds")
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 502
        assert resp.json()["message"] == "Discord OAuth request failed"
        user = client.app.state.db.query_one("SELECT is_active FROM users WHERE id = ?", (user_id,))
        assert user["is_active"] == 1
        assert client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"}).status_code == 200


def test_discord_signup_creates_group_user_and_shows_api_key_once(tmp_path: Path, monkeypatch):
    config_path, group_id = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)

        assert page.status_code == 200
        api_key = _extract_api_key(page.text)
        assert "Dc: Tester" in page.text
        assert client.get("/account").text.find("nai_proxy_") == -1

        user = client.app.state.db.query_one("SELECT id, name, group_id, api_key FROM users")
        assert user["name"] == "Dc: Tester"
        assert user["group_id"] == group_id
        assert user["api_key"] is None
        quota = client.app.state.db.query_one("SELECT total, reset_period, reset_day FROM user_anlas_quota WHERE user_id = ?", (user["id"],))
        assert dict(quota) == {"total": 42, "reset_period": "week", "reset_day": 2}
        link = client.app.state.db.query_one("SELECT discord_user_id, discord_username, discord_global_name FROM discord_user_links")
        assert dict(link) == {
            "discord_user_id": DISCORD_USER_ID,
            "discord_username": "tester",
            "discord_global_name": "Tester",
        }

        sub = client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"})
        assert sub.status_code == 200


def test_discord_registration_inherits_group_member_rate_limit_rules(tmp_path: Path, monkeypatch):
    config_path, group_id = _write_self_service_config(tmp_path)
    db = Database(str(tmp_path / "test.db"))
    now = utc_now_iso()
    for period, max_requests in (("minute", 3), ("hour", 60)):
        db.execute(
            """
            INSERT INTO group_member_rate_limit_rules (group_id, period, max_requests, is_active, created_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (group_id, period, max_requests, now),
        )
    db.close()
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]
        rules = client.app.state.db.query_all(
            "SELECT period, max_requests, is_active FROM rate_limit_rules WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        assert [dict(row) for row in rules] == [
            {"period": "minute", "max_requests": 3, "is_active": 1},
            {"period": "hour", "max_requests": 60, "is_active": 1},
        ]


def test_account_shows_group_daily_usage_and_hides_zero_anlas_quota(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(
        tmp_path,
        default_anlas_total=0,
        free_small_daily_limit_enabled=True,
        free_small_daily_limit=4,
    )
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]
        user_row = client.app.state.db.query_one(
            "SELECT free_small_daily_limit_enabled, free_small_daily_limit FROM users WHERE id = ?",
            (user_id,),
        )
        assert dict(user_row) == {"free_small_daily_limit_enabled": 1, "free_small_daily_limit": 4}

        daily_manager = client.app.state.free_small_daily_limit_manager
        used_reservation = daily_manager.reserve(user_id, 1)
        daily_manager.confirm(used_reservation)
        daily_manager.reserve(user_id, 1)

        page = client.get("/account")
        assert page.status_code == 200
        text = _normalized_text(page.text)
        assert "免费小图单日数量" in text
        assert "已用 1 锁定 1 上限 4 可用 2" in text
        assert "Anlas额度" not in text


def test_account_shows_positive_anlas_quota_snapshot(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]
        client.app.state.db.execute(
            "UPDATE user_anlas_quota SET used = 5, reserved = 3 WHERE user_id = ?",
            (user_id,),
        )

        page = client.get("/account")
        assert page.status_code == 200
        text = _normalized_text(page.text)
        assert "Anlas额度" in text
        assert "总额 42 已用 5 锁定 3 可用 34" in text
        assert "Anlas额度重置规则：每周第 2 天" in text


def test_account_queue_status_uses_live_snapshot_and_refresh_controls(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    class QueueSnapshot:
        def snapshot(self):
            return {
                "queue_size": 5,
                "running": None,
                "running_items": [{"request_id": "running-a"}, {"request_id": "running-b"}],
                "queued": [
                    {"request_id": "dispatch-a", "status": "dispatch_queued"},
                    {"request_id": "dispatch-b", "status": "dispatch_queued"},
                    {"request_id": "queued-a", "status": "queued"},
                    {"request_id": "queued-b", "status": "queued"},
                    {"request_id": "queued-c", "status": "queued"},
                ],
                "dispatch_queue_size": 2,
                "upstreams": [
                    {"id": "upstream-a", "queue_size": 1},
                    {"id": "upstream-b", "queue_size": 2},
                ],
            }

    with TestClient(app) as client:
        assert client.get("/account/api/queue-status").status_code == 401
        _complete_discord_login(client)
        client.app.state.proxy_queue = QueueSnapshot()

        page = client.get("/account")
        assert page.status_code == 200
        text = _normalized_text(page.text)
        assert "生成队列状态" in text
        assert "正在生成 2/2 正在排队 3/4" in text
        assert 'id="account-queue-refresh"' in page.text
        assert "/account/api/queue-status" in page.text

        status = client.get("/account/api/queue-status")
        assert status.status_code == 200
        assert status.json() == {
            "running_count": 2,
            "running_total": 2,
            "queued_count": 3,
            "queued_total": 4,
        }


def test_account_shows_daily_anlas_reset_without_zero_day(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(
        tmp_path,
        default_reset_period="day",
        default_reset_day=0,
    )
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)

        page = client.get("/account")
        assert page.status_code == 200
        text = _normalized_text(page.text)
        assert "Anlas额度重置规则：每天重置" in text
        assert "第 0 天" not in text


def test_account_updates_image_format_policy_and_generation_uses_it(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path, default_anlas_total=100)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        api_key = _extract_api_key(page.text)
        account = client.get("/account")
        assert account.status_code == 200
        assert 'name="image_format_policy"' in account.text
        assert "/account/image-format-policy" in account.text

        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]
        before = dict(
            client.app.state.db.query_one(
                """
                SELECT name, tier, free_small_only, allowed_endpoints, image_format_policy
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            )
        )

        update = client.post(
            "/account/image-format-policy",
            data=csrf_form(
                client,
                {"image_format_policy": "force_png"},
                cookie_name="novelai_proxy_self_service_csrf",
            ),
            follow_redirects=False,
        )
        assert update.status_code == 303
        assert update.headers["location"] == "/account"

        after = dict(
            client.app.state.db.query_one(
                """
                SELECT name, tier, free_small_only, allowed_endpoints, image_format_policy
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            )
        )
        assert after == before | {"image_format_policy": "force_png"}

        fake_upstream = FakeUpstream()
        client.app.state.upstream = fake_upstream
        payload = PAYLOAD | {"parameters": PAYLOAD["parameters"] | {"image_format": "jpeg"}}
        generated = client.post("/ai/generate-image", headers={"Authorization": f"Bearer {api_key}"}, json=payload)

        assert generated.status_code == 201
        assert fake_upstream.last_generate_payload["parameters"]["image_format"] == "png"


def test_discord_registration_rolls_back_user_and_quota_on_failure(tmp_path: Path, monkeypatch):
    db = Database(str(tmp_path / "rollback.db"))
    db.init_schema()
    group_id = _create_default_group(db)
    quota_manager = QuotaManager(db)
    original_create_quota = quota_manager.create_or_update_with_connection

    def create_quota_then_fail(conn, *args, **kwargs):
        original_create_quota(conn, *args, **kwargs)
        raise RuntimeError("forced registration failure")

    monkeypatch.setattr(quota_manager, "create_or_update_with_connection", create_quota_then_fail)

    with pytest.raises(RuntimeError, match="forced registration failure"):
        accounts.login_or_register_discord_user(
            db,
            quota_manager,
            default_group_id=group_id,
            profile=accounts.DiscordProfile(
                user_id="discord-rollback",
                username="rollback",
                global_name="Rollback",
                avatar=None,
            ),
        )

    assert _count_rows(db, "users") == 0
    assert _count_rows(db, "user_anlas_quota") == 0
    assert _count_rows(db, "discord_user_links") == 0
    db.close()


def test_discord_repeat_login_syncs_names_without_duplicate_or_overwriting_manual_name(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]
        client.app.state.db.execute("UPDATE users SET name = 'Discord: Tester' WHERE id = ?", (user_id,))

        _complete_discord_login(
            client,
            user={"id": DISCORD_USER_ID, "username": "tester2", "global_name": "Tester Two", "avatar": "avatar-2"},
        )
        assert _count_rows(client.app.state.db, "users") == 1
        user = client.app.state.db.query_one("SELECT name FROM users WHERE id = ?", (user_id,))
        assert user["name"] == "Dc: Tester Two"
        link = client.app.state.db.query_one("SELECT discord_username, discord_global_name, discord_avatar FROM discord_user_links")
        assert dict(link) == {
            "discord_username": "tester2",
            "discord_global_name": "Tester Two",
            "discord_avatar": "avatar-2",
        }

        client.app.state.db.execute("UPDATE users SET name = 'Manual Name' WHERE id = ?", (user_id,))
        _complete_discord_login(
            client,
            user={"id": DISCORD_USER_ID, "username": "tester3", "global_name": "Tester Three", "avatar": "avatar-3"},
        )
        user = client.app.state.db.query_one("SELECT name FROM users WHERE id = ?", (user_id,))
        assert user["name"] == "Manual Name"
        link = client.app.state.db.query_one("SELECT discord_username, discord_global_name FROM discord_user_links")
        assert dict(link) == {"discord_username": "tester3", "discord_global_name": "Tester Three"}


def test_self_service_api_key_flash_is_bound_to_current_user(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        first = _complete_discord_login(client, follow_redirects=False)
        assert first.status_code == 303
        first_flash_token = client.cookies.get(API_KEY_FLASH_COOKIE)
        assert first_flash_token

        second = _complete_discord_login(
            client,
            user={"id": SECOND_DISCORD_USER_ID, "username": "second", "global_name": "Second", "avatar": "avatar-2"},
            follow_redirects=False,
        )
        assert second.status_code == 303
        client.cookies.set(API_KEY_FLASH_COOKIE, first_flash_token, domain="testserver.local", path="/")

        page = client.get("/account")

        assert page.status_code == 200
        assert "Dc: Second" in page.text
        assert "nai_proxy_" not in page.text
        assert client.cookies.get(API_KEY_FLASH_COOKIE) is None
        assert _count_rows(client.app.state.db, "users") == 2


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


def test_disabled_discord_user_cannot_use_self_service_account(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]
        old_hash = client.app.state.db.query_one("SELECT api_key_hash FROM users WHERE id = ?", (user_id,))["api_key_hash"]
        client.app.state.db.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))

        account = client.get("/account")
        assert account.status_code == 403
        assert account.json()["message"] == "Account is disabled"

        reset = client.post(
            "/account/reset-key",
            data=csrf_form(client, cookie_name="novelai_proxy_self_service_csrf"),
        )
        assert reset.status_code == 403
        assert reset.json()["message"] == "Account is disabled"
        current_hash = client.app.state.db.query_one("SELECT api_key_hash FROM users WHERE id = ?", (user_id,))["api_key_hash"]
        assert current_hash == old_hash

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient()
        relogin = client.get(f"/auth/discord/callback?code=ok&state={state}")
        assert relogin.status_code == 403
        assert relogin.json()["message"] == "Account is disabled"


def test_self_service_reset_key_invalidates_old_key_and_does_not_store_discord_tokens(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        old_key = _extract_api_key(page.text)

        reset = client.post(
            "/account/reset-key",
            data=csrf_form(client, cookie_name="novelai_proxy_self_service_csrf"),
            follow_redirects=True,
        )
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


def _write_self_service_config(
    tmp_path: Path,
    *,
    default_anlas_total: int = 42,
    default_reset_period: str = "week",
    default_reset_day: int = 2,
    free_small_daily_limit_enabled: bool = False,
    free_small_daily_limit: int = 0,
    default_image_format_policy: str = "follow_global",
) -> tuple[Path, int]:
    config_path = write_test_config(tmp_path)
    db = Database(str(tmp_path / "test.db"))
    db.init_schema()
    group_id = _create_default_group(
        db,
        default_anlas_total=default_anlas_total,
        default_reset_period=default_reset_period,
        default_reset_day=default_reset_day,
        free_small_daily_limit_enabled=free_small_daily_limit_enabled,
        free_small_daily_limit=free_small_daily_limit,
        default_image_format_policy=default_image_format_policy,
    )
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
    required_guild_id: "{REQUIRED_GUILD_ID}"
    default_group_id: {group_id}
    session_secret: "test-session-secret"
"""
        )
    return config_path, group_id


def _create_default_group(
    db: Database,
    *,
    default_anlas_total: int = 42,
    default_reset_period: str = "week",
    default_reset_day: int = 2,
    free_small_daily_limit_enabled: bool = False,
    free_small_daily_limit: int = 0,
    default_image_format_policy: str = "follow_global",
) -> int:
    cursor = db.execute(
        """
        INSERT INTO user_groups (
            name, is_active, default_tier, default_free_small_only,
            free_small_daily_limit_enabled, free_small_daily_limit,
            default_allowed_endpoints, default_image_format_policy, default_anlas_total,
            default_reset_period, default_reset_day, created_at
        )
        VALUES ('discord-default', 1, 'vip', 0, ?, ?, 'generate-image', ?, ?, ?, ?, ?)
        """,
        (
            1 if free_small_daily_limit_enabled else 0,
            free_small_daily_limit,
            default_image_format_policy,
            default_anlas_total,
            default_reset_period,
            default_reset_day,
            utc_now_iso(),
        ),
    )
    return int(cursor.lastrowid)


def _complete_discord_login(client: TestClient, user: dict | None = None, *, follow_redirects: bool = True):
    state = _start_state(client)
    client.app.state.discord_oauth_client = FakeDiscordClient(user=user)
    return client.get(f"/auth/discord/callback?code=ok&state={state}", follow_redirects=follow_redirects)


def _start_state(client: TestClient) -> str:
    start = client.get("/auth/discord/start", follow_redirects=False)
    assert start.status_code == 303
    query = parse_qs(urlparse(start.headers["location"]).query)
    return query["state"][0]


def _extract_api_key(text: str) -> str:
    match = re.search(r"nai_proxy_[A-Za-z0-9_-]+", text)
    assert match is not None
    return match.group(0)


def _normalized_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def _count_rows(db: Database, table: str) -> int:
    return int(db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"])
