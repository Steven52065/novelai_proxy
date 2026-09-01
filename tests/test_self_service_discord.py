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
from app.self_service.discord import DiscordMemberNotFound
from app.self_service.routes import API_KEY_FLASH_COOKIE
from helpers import PAYLOAD, FakeUpstream, csrf_form, write_test_config


DISCORD_USER_ID = "100000000000000001"
SECOND_DISCORD_USER_ID = "100000000000000002"
REQUIRED_GUILD_ID = "200000000000000001"
OTHER_GUILD_ID = "200000000000000002"
REQUIRED_ROLE_ID = "300000000000000001"
SECOND_REQUIRED_ROLE_ID = "300000000000000003"
OTHER_ROLE_ID = "300000000000000002"


class FakeDiscordClient:
    def __init__(
        self,
        *,
        user: object | None = None,
        guilds: object | None = None,
        member: object | None = None,
        fail_at: str | None = None,
        member_not_found: bool = False,
    ):
        self.user = (
            user
            if user is not None
            else {"id": DISCORD_USER_ID, "username": "tester", "global_name": "Tester", "avatar": "avatar"}
        )
        self.guilds = guilds if guilds is not None else [{"id": REQUIRED_GUILD_ID}]
        self.member = member if member is not None else {"roles": [REQUIRED_ROLE_ID]}
        self.fail_at = fail_at
        self.member_not_found = member_not_found

    def authorization_url(self, *, redirect_uri: str, state: str, scope: str = "identify guilds") -> str:
        return f"https://discord.example/oauth?state={state}&redirect_uri={redirect_uri}&scope={scope}"

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

    async def fetch_guild_member(self, *, access_token: str, guild_id: str) -> object:
        if self.member_not_found:
            raise DiscordMemberNotFound()
        if self.fail_at == "member":
            raise RuntimeError("member failed")
        return self.member


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
        assert resp.json()["message"] == "Discord 自助服务未启用"


def test_discord_oauth_state_mismatch_rejects_without_creating_user(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        start = client.get("/auth/discord/start", follow_redirects=False)
        assert start.status_code == 303
        bad = client.get("/auth/discord/callback?code=ok&state=bad")

        assert bad.status_code == 400
        assert bad.json()["message"] == "Discord OAuth 状态无效"
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
        assert resp.json()["message"] == "该 Discord 用户不在要求的服务器中"
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
        assert resp.json()["message"] == "该 Discord 用户不在要求的服务器中"
        user = client.app.state.db.query_one("SELECT is_active FROM users WHERE id = ?", (user_id,))
        assert user["is_active"] == 0
        api_resp = client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"})
        assert api_resp.status_code == 403
        assert api_resp.json()["message"] == "账号已被禁用"
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
        assert resp.json()["message"] == "Discord OAuth 请求失败"
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
        assert resp.json()["message"] == "Discord OAuth 请求失败"
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
        assert resp.json()["message"] == "Discord OAuth 请求失败"
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


def test_account_shows_only_user_rate_limit_rules_with_session_auth(tmp_path: Path, monkeypatch):
    config_path, group_id = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        endpoint = "/account/api/rate-limit-rules"
        assert client.get(endpoint).status_code == 401

        _complete_discord_login(client)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]
        now = utc_now_iso()
        for period, max_requests, is_active in (
            ("minute", 3, 1),
            ("hour", 60, 0),
            ("month", 500, 1),
        ):
            client.app.state.db.execute(
                """
                INSERT INTO rate_limit_rules (user_id, period, max_requests, is_active, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, period, max_requests, is_active, now),
            )
        client.app.state.db.execute(
            """
            INSERT INTO group_rate_limit_rules (group_id, period, max_requests, is_active, created_at)
            VALUES (?, 'day', 999, 1, ?)
            """,
            (group_id, now),
        )

        page = client.get("/account")
        assert page.status_code == 200
        text = _normalized_text(page.text)
        assert "用户独享限流" in text
        assert "组共享限流" not in text
        assert "每分钟" in text
        assert "每小时" in text
        assert "每 30 天（滚动）" in text
        assert "每个周期 3 次" in text
        assert "每个周期 60 次" in text
        assert "每个周期 500 次" in text
        assert "已停用" in text
        assert "999" not in text
        assert page.text.count("data-rate-limit-rule=") == 3

        rules = client.get(endpoint)
        assert rules.status_code == 200
        assert rules.json() == {
            "rules": [
                {"period": "minute", "period_label": "每分钟", "max_requests": 3, "is_active": True},
                {"period": "hour", "period_label": "每小时", "max_requests": 60, "is_active": False},
                {"period": "month", "period_label": "每 30 天（滚动）", "max_requests": 500, "is_active": True},
            ]
        }


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
        assert "禁止提交合租账号" in text
        assert "提交账号仅限pst格式的API Key！" in text
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
            allow_new_registration=True,
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
        assert resp.json()["message"] == "账号已被删除，请联系管理员"


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
        assert account.json()["message"] == "账号已被禁用"

        reset = client.post(
            "/account/reset-key",
            data=csrf_form(client, cookie_name="novelai_proxy_self_service_csrf"),
        )
        assert reset.status_code == 403
        assert reset.json()["message"] == "账号已被禁用"
        current_hash = client.app.state.db.query_one("SELECT api_key_hash FROM users WHERE id = ?", (user_id,))["api_key_hash"]
        assert current_hash == old_hash

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient()
        relogin = client.get(f"/auth/discord/callback?code=ok&state={state}")
        assert relogin.status_code == 403
        assert relogin.json()["message"] == "账号已被禁用"


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


def test_discord_oauth_scope_requests_only_identify_when_no_verification(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path, require_guild=False)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        start = client.get("/auth/discord/start", follow_redirects=False)

        assert start.status_code == 303
        scope = parse_qs(urlparse(start.headers["location"]).query)["scope"][0]
        assert scope == "identify"


def test_discord_oauth_scope_requests_guilds_when_only_guild_verification(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        start = client.get("/auth/discord/start", follow_redirects=False)

        assert start.status_code == 303
        scope = parse_qs(urlparse(start.headers["location"]).query)["scope"][0]
        assert scope == "identify guilds"


def test_discord_oauth_scope_narrows_to_members_read_when_role_verification_enabled(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        start = client.get("/auth/discord/start", follow_redirects=False)

        assert start.status_code == 303
        scope = parse_qs(urlparse(start.headers["location"]).query)["scope"][0]
        # guilds.members.read 只暴露指定服务器的成员信息，不再索取用户的全部服务器列表
        assert scope == "identify guilds.members.read"
        assert "guilds " not in f"{scope} "


def test_discord_registration_without_verification_accepts_any_user(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path, require_guild=False)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        state = _start_state(client)
        # fail_at 覆盖两个验证端点：一旦被调用就会抛错，用来证明关闭验证后不会发起这些请求
        client.app.state.discord_oauth_client = FakeDiscordClient(
            guilds=[{"id": OTHER_GUILD_ID}],
            fail_at="guilds",
        )
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}", follow_redirects=True)

        assert resp.status_code == 200
        assert _count_rows(client.app.state.db, "users") == 1


def test_discord_registration_with_required_role_succeeds(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(
            member={"roles": [OTHER_ROLE_ID, REQUIRED_ROLE_ID]},
        )
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}", follow_redirects=True)

        assert resp.status_code == 200
        assert _extract_api_key(resp.text)
        assert _count_rows(client.app.state.db, "users") == 1


def test_discord_registration_accepts_any_one_of_the_required_roles(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(
        tmp_path,
        require_role=True,
        required_role_ids=[REQUIRED_ROLE_ID, SECOND_REQUIRED_ROLE_ID],
    )
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [SECOND_REQUIRED_ROLE_ID]})
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}", follow_redirects=True)

        assert resp.status_code == 200
        assert _count_rows(client.app.state.db, "users") == 1


@pytest.mark.parametrize("roles", [[OTHER_ROLE_ID], []])
def test_discord_user_without_required_role_is_rejected(tmp_path: Path, monkeypatch, roles: list[str]):
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": roles})
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 403
        assert resp.json()["message"] == "该 Discord 用户没有要求的身份组"
        assert _count_rows(client.app.state.db, "users") == 0


def test_discord_non_member_is_rejected_as_guild_failure_when_role_verification_enabled(
    tmp_path: Path,
    monkeypatch,
):
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        state = _start_state(client)
        # 成员接口在非成员时返回 404，等价于服务器验证失败
        client.app.state.discord_oauth_client = FakeDiscordClient(member_not_found=True)
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 403
        assert resp.json()["message"] == "该 Discord 用户不在要求的服务器中"
        assert _count_rows(client.app.state.db, "users") == 0


def test_discord_login_without_required_role_disables_existing_account(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        api_key = _extract_api_key(page.text)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [OTHER_ROLE_ID]})
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 403
        assert resp.json()["message"] == "该 Discord 用户没有要求的身份组"
        user = client.app.state.db.query_one("SELECT is_active FROM users WHERE id = ?", (user_id,))
        assert user["is_active"] == 0
        api_resp = client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"})
        assert api_resp.status_code == 403
        assert api_resp.json()["message"] == "账号已被禁用"
        assert client.get("/account").status_code == 403


@pytest.mark.parametrize(
    "member",
    [
        [{"roles": []}],
        {},
        {"roles": None},
        {"roles": REQUIRED_ROLE_ID},
        {"roles": [None]},
        {"roles": [300000000000000001]},
        {"roles": [True]},
        {"roles": [{"unexpected": True}]},
        {"roles": ["not-a-snowflake"]},
        {"roles": ["0300000000000000001"]},
        {"roles": [str(1 << 64)]},
        {"roles": [REQUIRED_ROLE_ID, {"unexpected": True}]},
    ],
)
def test_invalid_discord_member_response_does_not_disable_existing_account(
    tmp_path: Path,
    monkeypatch,
    member: object,
):
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        api_key = _extract_api_key(page.text)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member=member)
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        # 响应格式不可信属于“情况不明”，必须 502 且绝不停用账号
        assert resp.status_code == 502
        assert resp.json()["message"] == "Discord OAuth 请求失败"
        user = client.app.state.db.query_one("SELECT is_active FROM users WHERE id = ?", (user_id,))
        assert user["is_active"] == 1
        assert client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"}).status_code == 200


def test_failed_discord_member_request_does_not_disable_existing_account(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        api_key = _extract_api_key(page.text)
        user_id = client.app.state.db.query_one("SELECT id FROM users")["id"]

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(fail_at="member")
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 502
        assert resp.json()["message"] == "Discord OAuth 请求失败"
        user = client.app.state.db.query_one("SELECT is_active FROM users WHERE id = ?", (user_id,))
        assert user["is_active"] == 1
        assert client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"}).status_code == 200


def _verification_state(client: TestClient, user_id: int) -> tuple[int, int]:
    row = client.app.state.db.query_one(
        "SELECT is_active, disabled_by_discord_verification FROM users WHERE id = ?",
        (user_id,),
    )
    return int(row["is_active"]), int(row["disabled_by_discord_verification"])


def _admin_set_active(client: TestClient, user_id: int, is_active: bool) -> None:
    resp = client.patch(
        f"/admin/api/users/{user_id}",
        auth=("admin", "admin123"),
        json={"is_active": is_active},
    )
    assert resp.status_code == 200


def _admin_save_via_edit_form(
    client: TestClient,
    user_id: int,
    new_name: str,
    *,
    hard_disable: bool = False,
) -> None:
    """通过后台用户编辑表单保存一次，复现管理员在页面上点保存的真实请求。

    表单对停用中的账号不会提交 is_active（复选框未勾选），其余字段按页面回填当前值原样提交，
    因此默认这次保存并没有改动启用状态。hard_disable 对应页面上显式的“永久停用”开关。
    """
    login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
    assert login.status_code == 200
    row = client.app.state.db.query_one(
        """
        SELECT u.tier, u.is_active, u.group_id, u.allowed_endpoints, u.image_format_policy,
               q.total, q.reset_period, q.reset_day
        FROM users u
        LEFT JOIN user_anlas_quota q ON q.user_id = u.id
        WHERE u.id = ?
        """,
        (user_id,),
    )
    data: dict[str, object] = {
        "name": new_name,
        "tier": str(row["tier"]),
        "group_id": str(row["group_id"]),
        "anlas_total": str(int(row["total"] or 0)),
        "reset_period": str(row["reset_period"] or "month"),
        "reset_day": str(int(row["reset_day"] or 1)),
        "allowed_endpoints": str(row["allowed_endpoints"]).split(","),
        "image_format_policy": str(row["image_format_policy"]),
    }
    if int(row["is_active"]):
        data["is_active"] = "on"
    if hard_disable:
        data["hard_disable"] = "on"
    resp = client.post(
        f"/admin/users/{user_id}",
        data=csrf_form(client, data),
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_discord_login_reactivates_account_disabled_by_verification(tmp_path: Path, monkeypatch):
    """掉身份组被停用后，重新拿回身份组登录应自动恢复启用。"""
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        api_key = _extract_api_key(page.text)
        user_id = int(client.app.state.db.query_one("SELECT id FROM users")["id"])

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [OTHER_ROLE_ID]})
        assert client.get(f"/auth/discord/callback?code=ok&state={state}").status_code == 403
        assert _verification_state(client, user_id) == (0, 1)

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [REQUIRED_ROLE_ID]})
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}", follow_redirects=True)

        assert resp.status_code == 200
        assert _verification_state(client, user_id) == (1, 0)
        # 恢复后原 API Key 立即可用，不需要重置；也不应重复注册出第二个账号
        assert client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"}).status_code == 200
        assert _count_rows(client.app.state.db, "users") == 1


def test_admin_disabled_account_is_not_reactivated_by_passing_verification(tmp_path: Path, monkeypatch):
    """管理员手工停用的账号，即使用户通过验证也不得自动恢复，否则等于可以自助解封。"""
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        api_key = _extract_api_key(page.text)
        user_id = int(client.app.state.db.query_one("SELECT id FROM users")["id"])

        _admin_set_active(client, user_id, False)
        assert _verification_state(client, user_id) == (0, 0)

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [REQUIRED_ROLE_ID]})
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 403
        assert resp.json()["message"] == "账号已被禁用"
        assert _verification_state(client, user_id) == (0, 0)
        assert client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"}).status_code == 403


def test_admin_disable_after_verification_disable_is_not_undone_by_verification(tmp_path: Path, monkeypatch):
    """验证停用→管理员启用→管理员再停用后，通过验证也不得自动恢复。

    这是残留标记的回归用例：若管理员改动启用状态时不清除标记，
    管理员后来的停用会被用户下一次验证通过悄悄撤销。
    """
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)
        user_id = int(client.app.state.db.query_one("SELECT id FROM users")["id"])

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [OTHER_ROLE_ID]})
        assert client.get(f"/auth/discord/callback?code=ok&state={state}").status_code == 403
        assert _verification_state(client, user_id) == (0, 1)

        _admin_set_active(client, user_id, True)
        assert _verification_state(client, user_id) == (1, 0)
        _admin_set_active(client, user_id, False)
        assert _verification_state(client, user_id) == (0, 0)

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [REQUIRED_ROLE_ID]})
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 403
        assert resp.json()["message"] == "账号已被禁用"
        assert _verification_state(client, user_id) == (0, 0)


def test_admin_editing_other_fields_does_not_revoke_auto_recovery(tmp_path: Path, monkeypatch):
    """管理员在后台编辑表单里改别的字段，不得让验证停用的账号失去自动恢复能力。

    编辑表单每次保存都会原样回传当前启用状态，若据此认定“管理员手工停用”，
    管理员改个名字就会把验证停用悄悄升级成永久停用，用户重新拿回身份组也回不来。
    只有管理员真的改动了启用状态才算手工停用。
    """
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        api_key = _extract_api_key(page.text)
        user_id = int(client.app.state.db.query_one("SELECT id FROM users")["id"])

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [OTHER_ROLE_ID]})
        assert client.get(f"/auth/discord/callback?code=ok&state={state}").status_code == 403
        assert _verification_state(client, user_id) == (0, 1)

        _admin_save_via_edit_form(client, user_id, "管理员备注名")

        assert client.app.state.db.query_one("SELECT name FROM users WHERE id = ?", (user_id,))["name"] == "管理员备注名"
        assert _verification_state(client, user_id) == (0, 1)

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [REQUIRED_ROLE_ID]})
        assert client.get(f"/auth/discord/callback?code=ok&state={state}").status_code == 200
        assert _verification_state(client, user_id) == (1, 0)
        assert client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"}).status_code == 200


def test_admin_api_omitting_is_active_does_not_revoke_auto_recovery(tmp_path: Path, monkeypatch):
    """同上，但走 JSON 接口：请求里没有 is_active 时只改别的字段，不影响自动恢复。"""
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)
        user_id = int(client.app.state.db.query_one("SELECT id FROM users")["id"])

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [OTHER_ROLE_ID]})
        assert client.get(f"/auth/discord/callback?code=ok&state={state}").status_code == 403
        assert _verification_state(client, user_id) == (0, 1)

        resp = client.patch(
            f"/admin/api/users/{user_id}",
            auth=("admin", "admin123"),
            json={"name": "仅改名"},
        )
        assert resp.status_code == 200
        assert _verification_state(client, user_id) == (0, 1)

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [REQUIRED_ROLE_ID]})
        assert client.get(f"/auth/discord/callback?code=ok&state={state}").status_code == 200
        assert _verification_state(client, user_id) == (1, 0)


def test_admin_api_explicit_disable_is_treated_as_hard_ban(tmp_path: Path, monkeypatch):
    """管理 API 显式提交 is_active=false 即视为管理员手工停用，通过验证也不得自动恢复。

    管理 API 只在请求里真的出现 is_active 时才下发该字段，所以“显式提交”本身就是管理员意图，
    不需要再与当前值比对：即使账号此刻已经是停用状态，这一次提交也算管理员确认封禁。
    """
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)
        user_id = int(client.app.state.db.query_one("SELECT id FROM users")["id"])

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [OTHER_ROLE_ID]})
        assert client.get(f"/auth/discord/callback?code=ok&state={state}").status_code == 403
        assert _verification_state(client, user_id) == (0, 1)

        _admin_set_active(client, user_id, False)
        assert _verification_state(client, user_id) == (0, 0)

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [REQUIRED_ROLE_ID]})
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 403
        assert resp.json()["message"] == "账号已被禁用"
        assert _verification_state(client, user_id) == (0, 0)


def test_admin_hard_disable_via_edit_form_blocks_auto_recovery(tmp_path: Path, monkeypatch):
    """后台编辑表单勾选“永久停用”后，账号不再因通过 Discord 验证而自动恢复。

    表单里“未勾选启用”对停用账号来说和“没碰这个字段”无法区分，因此提供独立的开关，
    让管理员在不先启用再停用的前提下也能一步确认封禁。
    """
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        api_key = _extract_api_key(page.text)
        user_id = int(client.app.state.db.query_one("SELECT id FROM users")["id"])

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [OTHER_ROLE_ID]})
        assert client.get(f"/auth/discord/callback?code=ok&state={state}").status_code == 403
        assert _verification_state(client, user_id) == (0, 1)

        _admin_save_via_edit_form(client, user_id, "封禁用户", hard_disable=True)
        assert _verification_state(client, user_id) == (0, 0)

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [REQUIRED_ROLE_ID]})
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 403
        assert resp.json()["message"] == "账号已被禁用"
        assert _verification_state(client, user_id) == (0, 0)
        assert client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"}).status_code == 403


def test_user_edit_page_shows_hard_disable_only_for_verification_disabled_account(tmp_path: Path, monkeypatch):
    """“永久停用”开关只对因验证停用的账号出现，否则管理员看不出该账号会自动恢复。"""
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)
        user_id = int(client.app.state.db.query_one("SELECT id FROM users")["id"])
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        page = client.get(f"/admin/users/{user_id}")
        assert page.status_code == 200
        assert "hard_disable" not in page.text

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [OTHER_ROLE_ID]})
        assert client.get(f"/auth/discord/callback?code=ok&state={state}").status_code == 403
        assert _verification_state(client, user_id) == (0, 1)

        page = client.get(f"/admin/users/{user_id}")
        assert page.status_code == 200
        assert "hard_disable" in page.text
        assert "永久停用" in page.text

        _admin_set_active(client, user_id, True)
        assert _verification_state(client, user_id) == (1, 0)

        page = client.get(f"/admin/users/{user_id}")
        assert page.status_code == 200
        assert "hard_disable" not in page.text


def test_admin_can_enable_account_disabled_by_verification(tmp_path: Path, monkeypatch):
    """被验证停用的账号必须仍然可以由管理员手工启用。"""
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = _complete_discord_login(client)
        api_key = _extract_api_key(page.text)
        user_id = int(client.app.state.db.query_one("SELECT id FROM users")["id"])

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [OTHER_ROLE_ID]})
        assert client.get(f"/auth/discord/callback?code=ok&state={state}").status_code == 403
        assert client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"}).status_code == 403

        _admin_set_active(client, user_id, True)

        assert _verification_state(client, user_id) == (1, 0)
        assert client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"}).status_code == 200


def test_account_disabled_by_verification_recovers_when_verification_is_disabled(tmp_path: Path, monkeypatch):
    """关闭验证后验证平凡通过，之前被验证停用的账号下次登录即自动恢复。"""
    config_path, _ = _write_self_service_config(tmp_path, require_guild=False)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)
        user_id = int(client.app.state.db.query_one("SELECT id FROM users")["id"])
        # 等价于关闭验证开关之前那一轮验证失败造成的停用
        accounts.disable_linked_discord_user(client.app.state.db, discord_user_id=DISCORD_USER_ID)
        assert _verification_state(client, user_id) == (0, 1)

        resp = _complete_discord_login(client)

        assert resp.status_code == 200
        assert _verification_state(client, user_id) == (1, 0)


def test_account_disabled_before_upgrade_is_not_reactivated(tmp_path: Path, monkeypatch):
    """升级前停用的存量账号没有来源标记，必须按管理员停用处理，不得被自动放出来。"""
    config_path, _ = _write_self_service_config(tmp_path, require_role=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)
        user_id = int(client.app.state.db.query_one("SELECT id FROM users")["id"])
        # 旧版本只会写 is_active，不会写来源标记
        client.app.state.db.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
        assert _verification_state(client, user_id) == (0, 0)

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [REQUIRED_ROLE_ID]})
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 403
        assert resp.json()["message"] == "账号已被禁用"
        assert _verification_state(client, user_id) == (0, 0)


def test_discord_registration_closed_rejects_new_user(tmp_path: Path, monkeypatch):
    """关闭注册后，全新 Discord 用户回调阶段被拒绝且不建号。"""
    config_path, _ = _write_self_service_config(tmp_path, disable_new_registration=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient()
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}")

        assert resp.status_code == 403
        assert resp.json()["message"] == "自助注册已关闭，仅允许已注册用户登录"
        assert _count_rows(client.app.state.db, "users") == 0
        assert _count_rows(client.app.state.db, "discord_user_links") == 0


def test_discord_registration_closed_allows_existing_user_login(tmp_path: Path, monkeypatch):
    """已注册老用户在关闭注册后仍可登录，不产生第二个用户。"""
    config_path, _ = _write_self_service_config(tmp_path, disable_new_registration=False)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        first = _complete_discord_login(client, follow_redirects=False)
        assert first.status_code == 303
        assert _count_rows(client.app.state.db, "users") == 1

        client.app.state.config.self_service.discord.disable_new_registration = True

        second = _complete_discord_login(client, follow_redirects=False)
        assert second.status_code == 303
        assert second.headers["location"] == "/account"
        assert client.get("/account").status_code == 200
        assert _count_rows(client.app.state.db, "users") == 1
        assert _count_rows(client.app.state.db, "discord_user_links") == 1


def test_discord_registration_closed_still_recovers_verification_disabled_user(tmp_path: Path, monkeypatch):
    """关闭注册不影响因验证失败停用账号的恢复。"""
    config_path, _ = _write_self_service_config(tmp_path, require_role=True, disable_new_registration=False)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _complete_discord_login(client)
        user_id = int(client.app.state.db.query_one("SELECT id FROM users")["id"])
        client.app.state.config.self_service.discord.disable_new_registration = True

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [OTHER_ROLE_ID]})
        assert client.get(f"/auth/discord/callback?code=ok&state={state}").status_code == 403
        assert _verification_state(client, user_id) == (0, 1)

        state = _start_state(client)
        client.app.state.discord_oauth_client = FakeDiscordClient(member={"roles": [REQUIRED_ROLE_ID]})
        resp = client.get(f"/auth/discord/callback?code=ok&state={state}", follow_redirects=True)

        assert resp.status_code == 200
        assert _verification_state(client, user_id) == (1, 0)
        assert _count_rows(client.app.state.db, "users") == 1


def test_signup_page_shows_registration_closed_hint(tmp_path: Path, monkeypatch):
    """开启关闭注册后 /signup 页面展示关闭提示。"""
    config_path, _ = _write_self_service_config(tmp_path, disable_new_registration=True)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        page = client.get("/signup")

        assert page.status_code == 200
        assert "当前已关闭新用户注册，仅限已注册用户登录" in page.text


def _write_self_service_config(
    tmp_path: Path,
    *,
    default_anlas_total: int = 42,
    default_reset_period: str = "week",
    default_reset_day: int = 2,
    free_small_daily_limit_enabled: bool = False,
    free_small_daily_limit: int = 0,
    default_image_format_policy: str = "follow_global",
    require_guild: bool = True,
    require_role: bool = False,
    required_role_ids: list[str] | None = None,
    disable_new_registration: bool = False,
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
    role_ids = required_role_ids if required_role_ids is not None else [REQUIRED_ROLE_ID]
    formatted_role_ids = ", ".join(f'"{role_id}"' for role_id in role_ids)
    with config_path.open("a", encoding="utf-8") as f:
        f.write(
            f"""
self_service:
  discord:
    enabled: true
    client_id: "client-id"
    client_secret: "client-secret"
    redirect_uri: "http://testserver/auth/discord/callback"
    require_guild: {"true" if require_guild else "false"}
    required_guild_id: "{REQUIRED_GUILD_ID}"
    require_role: {"true" if require_role else "false"}
    required_role_ids: [{formatted_role_ids}]
    disable_new_registration: {"true" if disable_new_registration else "false"}
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
