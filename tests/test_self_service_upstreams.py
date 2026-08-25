from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.allowlists import AllowedUpstreams
from app.database import Database, utc_now_iso
from app.self_service.discord import DiscordMemberNotFound
from app.upstreams import mask_token
from app.users import reset_api_key
from helpers import PAYLOAD, FakeUpstream, csrf_headers, write_test_config


DISCORD_USER_ID = "100000000000000001"
SECOND_DISCORD_USER_ID = "100000000000000002"
REQUIRED_GUILD_ID = "200000000000000001"
REQUIRED_ROLE_ID = "300000000000000001"

SELF_SERVICE_CSRF = "novelai_proxy_self_service_csrf"
LONG_TOKEN = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP"
LONG_MASKED = mask_token(LONG_TOKEN)


class FakeDiscordClient:
    def __init__(self, *, user: object | None = None, member: object | None = None):
        self.user = (
            user
            if user is not None
            else {"id": DISCORD_USER_ID, "username": "tester", "global_name": "Tester", "avatar": "avatar"}
        )
        self.guilds = [{"id": REQUIRED_GUILD_ID}]
        self.member = member if member is not None else {"roles": [REQUIRED_ROLE_ID]}

    def authorization_url(self, *, redirect_uri: str, state: str, scope: str = "identify guilds") -> str:
        return f"https://discord.example/oauth?state={state}&redirect_uri={redirect_uri}&scope={scope}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        return {"access_token": "secret-access-token", "refresh_token": "secret-refresh-token"}

    async def fetch_user(self, *, access_token: str) -> object:
        return self.user

    async def fetch_guilds(self, *, access_token: str) -> object:
        return self.guilds

    async def fetch_guild_member(self, *, access_token: str, guild_id: str) -> object:
        if self.member is None:
            raise DiscordMemberNotFound()
        return self.member


def _write_self_service_config(
    tmp_path: Path,
    *,
    discord_enabled: bool = True,
    upstreams_enabled: bool = True,
    max_per_user: int = 5,
    default_anlas_total: int = 42,
) -> tuple[Path, int]:
    config_path = write_test_config(tmp_path)
    db = Database(str(tmp_path / "test.db"))
    db.init_schema()
    cursor = db.execute(
        """
        INSERT INTO user_groups (
            name, is_active, default_tier, default_free_small_only,
            free_small_daily_limit_enabled, free_small_daily_limit,
            default_allowed_endpoints, default_image_format_policy, default_anlas_total,
            default_reset_period, default_reset_day, created_at
        )
        VALUES ('discord-default', 1, 'vip', 0, 0, 0, 'generate-image', 'follow_global', ?, 'week', 2, ?)
        """,
        (default_anlas_total, utc_now_iso()),
    )
    group_id = int(cursor.lastrowid)
    db.close()
    with config_path.open("a", encoding="utf-8") as f:
        f.write(
            f"""
self_service:
  discord:
    enabled: {"true" if discord_enabled else "false"}
    client_id: "client-id"
    client_secret: "client-secret"
    redirect_uri: "http://testserver/auth/discord/callback"
    require_guild: true
    required_guild_id: "{REQUIRED_GUILD_ID}"
    require_role: false
    required_role_ids: ["{REQUIRED_ROLE_ID}"]
    default_group_id: {group_id}
    session_secret: "test-session-secret"
  upstreams:
    enabled: {"true" if upstreams_enabled else "false"}
    max_per_user: {max_per_user}
"""
        )
    return config_path, group_id


def _complete_discord_login(client: TestClient, user: dict | None = None, *, follow_redirects: bool = True):
    state = _start_state(client)
    client.app.state.discord_oauth_client = FakeDiscordClient(user=user)
    return client.get(f"/auth/discord/callback?code=ok&state={state}", follow_redirects=follow_redirects)


def _start_state(client: TestClient) -> str:
    start = client.get("/auth/discord/start", follow_redirects=False)
    assert start.status_code == 303
    query = parse_qs(urlparse(start.headers["location"]).query)
    return query["state"][0]


def _login(client: TestClient, *, user_id: str, username: str) -> int:
    _complete_discord_login(
        client,
        {"id": user_id, "username": username, "global_name": username, "avatar": "avatar"},
    )
    return int(client.app.state.db.query_one("SELECT id FROM users WHERE name = ?", (f"Dc: {username}",))["id"])


def _csrf(client: TestClient) -> dict[str, str]:
    return csrf_headers(client, cookie_name=SELF_SERVICE_CSRF)


def _create_upstream(client: TestClient, *, label: str = "", api_key: str = LONG_TOKEN, enabled: bool = True) -> dict:
    resp = client.post(
        "/account/api/upstreams",
        headers=_csrf(client),
        json={"label": label, "api_key": api_key, "enabled": enabled},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["upstream"]


def _count_upstreams(db: Database) -> int:
    return int(db.query_one("SELECT COUNT(*) AS c FROM novelai_upstreams")["c"])


def _set_allowed_upstreams(db: Database, user_id: int, upstream_ids: list[str]) -> None:
    db.execute(
        "UPDATE users SET allowed_upstreams = ? WHERE id = ?",
        (AllowedUpstreams.of(upstream_ids).serialize(), user_id),
    )

# ---------- 安全边界（最高优先级） ----------


def test_user_cannot_patch_other_users_upstream(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        uid_a = _login(client, user_id=DISCORD_USER_ID, username="u1")
        upstream = _create_upstream(client, label="main", api_key="secret-token-a")
        _login(client, user_id=SECOND_DISCORD_USER_ID, username="u2")

        resp = client.patch(
            f"/account/api/upstreams/{upstream['id']}",
            headers=_csrf(client),
            json={"api_key": "stolen-token"},
        )

        assert resp.status_code == 404
        assert resp.json()["message"] == "上游不存在"
        row = client.app.state.db.query_one(
            "SELECT api_key, owner_user_id FROM novelai_upstreams WHERE id = ?",
            (upstream["id"],),
        )
        assert row["api_key"] == "secret-token-a"
        assert row["owner_user_id"] == uid_a


def test_user_cannot_delete_other_users_upstream(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        upstream = _create_upstream(client, label="main", api_key="secret-token-a")
        _login(client, user_id=SECOND_DISCORD_USER_ID, username="u2")

        resp = client.delete(f"/account/api/upstreams/{upstream['id']}", headers=_csrf(client))

        assert resp.status_code == 404
        assert client.app.state.db.query_one(
            "SELECT 1 FROM novelai_upstreams WHERE id = ?", (upstream["id"],)
        ) is not None


def test_user_cannot_manage_admin_owned_upstream(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        created = client.post(
            "/admin/api/upstreams",
            auth=("admin", "admin123"),
            json={"id": "admin-key", "api_key": "admin-token"},
        )
        assert created.status_code == 200
        _login(client, user_id=DISCORD_USER_ID, username="u1")

        patch = client.patch(
            "/account/api/upstreams/admin-key",
            headers=_csrf(client),
            json={"api_key": "stolen"},
        )
        delete = client.delete("/account/api/upstreams/admin-key", headers=_csrf(client))

        assert patch.status_code == 404
        assert delete.status_code == 404
        row = client.app.state.db.query_one("SELECT api_key FROM novelai_upstreams WHERE id = 'admin-key'")
        assert row["api_key"] == "admin-token"


def test_user_cannot_manage_literal_same_prefix_admin_key(tmp_path: Path, monkeypatch):
    """管理员手建字面 u12-x（owner NULL），用户 12 尝试 PATCH → 404：归属只认 owner_user_id 列。"""
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        db = client.app.state.db
        for index in range(11):
            db.execute(
                "INSERT INTO users(api_key_hash, name, created_at) VALUES (?, ?, datetime('now'))",
                (f"dummy-{index}", f"dummy-{index}"),
            )
        _login(client, user_id=DISCORD_USER_ID, username="u12")
        uid = int(db.query_one("SELECT id FROM users WHERE name = 'Dc: u12'")["id"])
        assert uid == 12

        created = client.post(
            "/admin/api/upstreams",
            auth=("admin", "admin123"),
            json={"id": "u12-x", "api_key": "admin-token"},
        )
        assert created.status_code == 200

        resp = client.patch(
            "/account/api/upstreams/u12-x",
            headers=_csrf(client),
            json={"api_key": "stolen"},
        )

        assert resp.status_code == 404
        assert resp.json()["message"] == "上游不存在"
        row = db.query_one("SELECT api_key, owner_user_id FROM novelai_upstreams WHERE id = 'u12-x'")
        assert row["api_key"] == "admin-token"
        assert row["owner_user_id"] is None


def test_label_mimicking_other_user_prefix_rejected_and_encoded_slash_404(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        upstream = _create_upstream(client, label="x", api_key="secret-token-a")

        resp = client.post(
            "/account/api/upstreams",
            headers=_csrf(client),
            json={"label": "u13-steal", "api_key": "whatever"},
        )
        assert resp.status_code == 400
        assert "前缀混淆" in resp.json()["message"]

        # %2F 注入在路由层直接 404，不会进入归属校验。
        encoded = client.patch(
            f"/account/api/upstreams/{upstream['id']}%2Ffoo",
            headers=_csrf(client),
            json={"api_key": "stolen"},
        )
        assert encoded.status_code == 404
        row = client.app.state.db.query_one(
            "SELECT api_key FROM novelai_upstreams WHERE id = ?", (upstream["id"],)
        )
        assert row["api_key"] == "secret-token-a"


def test_invalid_labels_rejected_with_chinese_message(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        for label in ["../", "..", ".", "#", "x\x00y", "x\ny", "x" * 33]:
            resp = client.post(
                "/account/api/upstreams",
                headers=_csrf(client),
                json={"label": label, "api_key": "whatever"},
            )
            assert resp.status_code == 400, (label, resp.text)
            assert resp.json()["message"]


def test_unauthenticated_requests_rejected_without_db_writes(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        upstream = _create_upstream(client, label="main", api_key="secret-token-a")
        # 清掉会话，模拟未登录
        client.cookies.delete("novelai_proxy_self_service_session")

        get = client.get("/account/api/upstreams")
        post = client.post("/account/api/upstreams", json={"api_key": "x"})
        patch = client.patch(f"/account/api/upstreams/{upstream['id']}", json={"api_key": "x"})
        delete = client.delete(f"/account/api/upstreams/{upstream['id']}")

        assert get.status_code == 401 and get.json()["message"] == "需要登录"
        assert post.status_code == 401
        assert patch.status_code == 401
        assert delete.status_code == 401
        # 只有启动时种子的 default 和登录后创建的那一个 key，未登录操作没有产生任何写入
        assert _count_upstreams(client.app.state.db) == 2

def test_disabled_account_rejected(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        uid = int(client.app.state.db.query_one("SELECT id FROM users WHERE name = 'Dc: u1'")["id"])
        upstream = _create_upstream(client, label="main", api_key="secret-token-a")
        client.app.state.db.execute("UPDATE users SET is_active = 0 WHERE id = ?", (uid,))

        for method in ("get", "post", "patch", "delete"):
            if method == "get":
                resp = client.get("/account/api/upstreams")
            elif method == "post":
                resp = client.post("/account/api/upstreams", headers=_csrf(client), json={"api_key": "x"})
            elif method == "patch":
                resp = client.patch(f"/account/api/upstreams/{upstream['id']}", headers=_csrf(client), json={"enabled": False})
            else:
                resp = client.delete(f"/account/api/upstreams/{upstream['id']}", headers=_csrf(client))
            assert resp.status_code == 403, (method, resp.text)
            assert resp.json()["message"] == "账号已被禁用"


def test_deleted_account_rejected(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        uid = int(client.app.state.db.query_one("SELECT id FROM users WHERE name = 'Dc: u1'")["id"])
        upstream = _create_upstream(client, label="main", api_key="secret-token-a")
        client.app.state.db.execute("UPDATE users SET deleted_at = ? WHERE id = ?", (utc_now_iso(), uid))

        get = client.get("/account/api/upstreams")
        patch = client.patch(f"/account/api/upstreams/{upstream['id']}", headers=_csrf(client), json={"enabled": False})

        assert get.status_code == 403 and get.json()["message"] == "账号不可用"
        assert patch.status_code == 403 and patch.json()["message"] == "账号不可用"


def test_write_operations_require_csrf(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        upstream = _create_upstream(client, label="main", api_key="secret-token-a")

        post = client.post("/account/api/upstreams", json={"api_key": "x"})
        patch = client.patch(f"/account/api/upstreams/{upstream['id']}", json={"enabled": False})
        delete = client.delete(f"/account/api/upstreams/{upstream['id']}")

        assert post.status_code == 403
        assert patch.status_code == 403
        assert delete.status_code == 403


def test_list_returns_only_own_upstreams(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        _create_upstream(client, label="main", api_key=LONG_TOKEN)
        _login(client, user_id=SECOND_DISCORD_USER_ID, username="u2")

        resp = client.get("/account/api/upstreams")

        assert resp.status_code == 200
        assert resp.json() == {"upstreams": []}
        assert LONG_MASKED not in resp.text


def test_plaintext_token_never_appears_in_self_service_responses(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        upstream = _create_upstream(client, label="main", api_key=LONG_TOKEN)
        assert LONG_TOKEN not in client.get("/account/api/upstreams").text

        patch = client.patch(
            f"/account/api/upstreams/{upstream['id']}",
            headers=_csrf(client),
            json={"api_key": "SECRET-TOKEN-abcdef1234567890XYZ"},
        )
        assert patch.status_code == 200
        assert "SECRET-TOKEN-abcdef1234567890XYZ" not in patch.text
        assert "SECRET-TOKEN-abcdef1234567890XYZ" not in client.get("/account/api/upstreams").text


def test_delete_conflict_hides_references(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        uid_a = int(client.app.state.db.query_one("SELECT id FROM users WHERE name = 'Dc: u1'")["id"])
        upstream = _create_upstream(client, label="main", api_key="secret-token-a")
        _login(client, user_id=SECOND_DISCORD_USER_ID, username="u2")
        uid_b = int(client.app.state.db.query_one("SELECT id FROM users WHERE name = 'Dc: u2'")["id"])
        _set_allowed_upstreams(client.app.state.db, uid_b, [upstream["id"]])

        # 回到用户 A 再删除
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        resp = client.delete(f"/account/api/upstreams/{upstream['id']}", headers=_csrf(client))

        assert resp.status_code == 409
        assert resp.json()["message"] == "该上游已被管理员指定给特定用户使用，暂时无法删除。你可以先停用它，或联系管理员。"
        assert "references" not in resp.text
        assert str(uid_b) not in resp.text
        assert client.app.state.db.query_one(
            "SELECT 1 FROM novelai_upstreams WHERE id = ?", (upstream["id"],)
        ) is not None
        notification = client.app.state.db.query_one(
            "SELECT COUNT(*) AS c FROM admin_notifications WHERE event_type = 'self_service_upstream_referenced'"
        )
        assert notification["c"] == 1


def test_disable_with_references_notifies_admin(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        upstream = _create_upstream(client, label="main", api_key="secret-token-a")
        _login(client, user_id=SECOND_DISCORD_USER_ID, username="u2")
        uid_b = int(client.app.state.db.query_one("SELECT id FROM users WHERE name = 'Dc: u2'")["id"])
        _set_allowed_upstreams(client.app.state.db, uid_b, [upstream["id"]])
        _login(client, user_id=DISCORD_USER_ID, username="u1")

        resp = client.patch(
            f"/account/api/upstreams/{upstream['id']}",
            headers=_csrf(client),
            json={"enabled": False},
        )

        assert resp.status_code == 200
        notification = client.app.state.db.query_one(
            "SELECT COUNT(*) AS c FROM admin_notifications WHERE event_type = 'self_service_upstream_referenced'"
        )
        assert notification["c"] == 1


def test_upstreams_disabled_by_config_returns_404(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path, upstreams_enabled=False)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        for method in ("get", "post", "patch", "delete"):
            if method == "get":
                resp = client.get("/account/api/upstreams")
            elif method == "post":
                resp = client.post("/account/api/upstreams", headers=_csrf(client), json={"api_key": "x"})
            elif method == "patch":
                resp = client.patch("/account/api/upstreams/u1-x", headers=_csrf(client), json={"enabled": False})
            else:
                resp = client.delete("/account/api/upstreams/u1-x", headers=_csrf(client))
            assert resp.status_code == 404, (method, resp.text)
            assert resp.json()["message"] == "自助上游上传未启用"


def test_discord_disabled_returns_404(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path, discord_enabled=False)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        get = client.get("/account/api/upstreams")
        post = client.post("/account/api/upstreams", json={"api_key": "x"})

        assert get.status_code == 404
        assert get.json()["message"] == "Discord 自助服务未启用"
        assert post.status_code == 404

# ---------- 功能正向 ----------


def test_create_with_label_sets_id_and_owner(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        uid = int(client.app.state.db.query_one("SELECT id FROM users WHERE name = 'Dc: u1'")["id"])

        upstream = _create_upstream(client, label="主号", api_key="secret-token-a")

        assert upstream["id"] == f"u{uid}-主号"
        assert upstream["owner_user_id"] == uid
        assert upstream["enabled"] is True
        row = client.app.state.db.query_one(
            "SELECT owner_user_id FROM novelai_upstreams WHERE id = ?", (upstream["id"],)
        )
        assert row["owner_user_id"] == uid


def test_auto_numbering_and_reuse(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        uid = int(client.app.state.db.query_one("SELECT id FROM users WHERE name = 'Dc: u1'")["id"])

        first = _create_upstream(client, api_key="secret-token-1")
        second = _create_upstream(client, api_key="secret-token-2")
        assert first["id"] == f"u{uid}-1"
        assert second["id"] == f"u{uid}-2"

        client.delete(f"/account/api/upstreams/{second['id']}", headers=_csrf(client))
        third = _create_upstream(client, api_key="secret-token-3")
        assert third["id"] == f"u{uid}-2"


def test_auto_numbering_skips_admin_occupied_prefix(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        uid = int(client.app.state.db.query_one("SELECT id FROM users WHERE name = 'Dc: u1'")["id"])
        _create_upstream(client, api_key="secret-token-1")
        _create_upstream(client, api_key="secret-token-2")
        created = client.post(
            "/admin/api/upstreams",
            auth=("admin", "admin123"),
            json={"id": f"u{uid}-3", "api_key": "admin-token"},
        )
        assert created.status_code == 200

        fourth = _create_upstream(client, api_key="secret-token-4")
        assert fourth["id"] == f"u{uid}-4"


def test_duplicate_label_conflicts(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        uid = int(client.app.state.db.query_one("SELECT id FROM users WHERE name = 'Dc: u1'")["id"])
        _create_upstream(client, label="主号", api_key="secret-token-a")

        resp = client.post(
            "/account/api/upstreams",
            headers=_csrf(client),
            json={"label": "主号", "api_key": "secret-token-b"},
        )

        assert resp.status_code == 409
        assert resp.json()["message"] == f"上游 id 已存在：u{uid}-主号"


def test_update_token_keeps_id_and_replaces_runtime_client(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        upstream = _create_upstream(client, label="main", api_key="secret-token-a")
        before = app.state.upstream_clients[upstream["id"]]

        resp = client.patch(
            f"/account/api/upstreams/{upstream['id']}",
            headers=_csrf(client),
            json={"api_key": "secret-token-b"},
        )

        assert resp.status_code == 200
        assert resp.json()["upstream"]["id"] == upstream["id"]
        assert "secret-token-b" not in resp.text
        assert app.state.upstream_clients[upstream["id"]] is not before
        assert app.state.upstream_clients[upstream["id"]].api_key == "secret-token-b"


def test_toggle_and_delete_sync_runtime(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        upstream = _create_upstream(client, label="main", api_key="secret-token-a")
        assert upstream["id"] in app.state.upstream_clients

        resp = client.patch(
            f"/account/api/upstreams/{upstream['id']}",
            headers=_csrf(client),
            json={"enabled": False},
        )
        assert resp.status_code == 200
        assert upstream["id"] not in app.state.upstream_clients

        resp = client.patch(
            f"/account/api/upstreams/{upstream['id']}",
            headers=_csrf(client),
            json={"enabled": True},
        )
        assert resp.status_code == 200
        assert upstream["id"] in app.state.upstream_clients

        resp = client.delete(f"/account/api/upstreams/{upstream['id']}", headers=_csrf(client))
        assert resp.status_code == 200
        assert client.app.state.db.query_one(
            "SELECT 1 FROM novelai_upstreams WHERE id = ?", (upstream["id"],)
        ) is None
        assert upstream["id"] not in app.state.upstream_clients


def test_max_per_user_limit(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path, max_per_user=2)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        _create_upstream(client, api_key="secret-token-1")
        _create_upstream(client, api_key="secret-token-2")

        resp = client.post(
            "/account/api/upstreams",
            headers=_csrf(client),
            json={"api_key": "secret-token-3"},
        )

        assert resp.status_code == 400
        assert resp.json()["message"] == "最多只能上传 2 个上游账号"


def test_max_per_user_zero_blocks_all(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path, max_per_user=0)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        resp = client.post(
            "/account/api/upstreams",
            headers=_csrf(client),
            json={"api_key": "secret-token-1"},
        )

        assert resp.status_code == 400
        assert resp.json()["message"] == "最多只能上传 0 个上游账号"


def test_other_user_can_generate_through_pool(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path, default_anlas_total=10000)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        upstream = _create_upstream(client, label="pool", api_key="secret-token-a")
        fake = FakeUpstream()
        app.state.upstream_clients[upstream["id"]] = fake

        _login(client, user_id=SECOND_DISCORD_USER_ID, username="u2")
        db = client.app.state.db
        uid_b = int(db.query_one("SELECT id FROM users WHERE name = 'Dc: u2'")["id"])
        _set_allowed_upstreams(db, uid_b, [upstream["id"]])
        api_key = reset_api_key(db, uid_b)

        resp = client.post(
            "/ai/generate-image",
            headers={"Authorization": f"Bearer {api_key}"},
            json=PAYLOAD,
        )

        assert resp.status_code == 201, resp.text
        assert fake.last_generate_payload is not None


def test_owner_disable_and_delete_keep_key_serving(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path, default_anlas_total=10000)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        db = client.app.state.db
        uid_a = int(db.query_one("SELECT id FROM users WHERE name = 'Dc: u1'")["id"])
        upstream = _create_upstream(client, label="pool", api_key="secret-token-a")
        fake = FakeUpstream()
        app.state.upstream_clients[upstream["id"]] = fake

        _login(client, user_id=SECOND_DISCORD_USER_ID, username="u2")
        uid_b = int(db.query_one("SELECT id FROM users WHERE name = 'Dc: u2'")["id"])
        _set_allowed_upstreams(db, uid_b, [upstream["id"]])
        api_key = reset_api_key(db, uid_b)

        # 停用上传者后，key 仍在运行态且可被他人使用
        db.execute("UPDATE users SET is_active = 0 WHERE id = ?", (uid_a,))
        resp = client.post(
            "/ai/generate-image",
            headers={"Authorization": f"Bearer {api_key}"},
            json=PAYLOAD,
        )
        assert resp.status_code == 201, resp.text
        assert fake.last_generate_payload is not None
        assert upstream["id"] in app.state.upstream_clients

        # 软删除上传者后同样保持可用
        fake.last_generate_payload = None
        db.execute("UPDATE users SET deleted_at = ? WHERE id = ?", (utc_now_iso(), uid_a))
        resp = client.post(
            "/ai/generate-image",
            headers={"Authorization": f"Bearer {api_key}"},
            json=PAYLOAD,
        )
        assert resp.status_code == 201, resp.text
        assert fake.last_generate_payload is not None
        assert upstream["id"] in app.state.upstream_clients

# ---------- 迁移与管理端 ----------


def test_existing_db_migrates_owner_user_id_column(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        columns = [
            row["name"]
            for row in client.app.state.db.query_all("PRAGMA table_info(novelai_upstreams)")
        ]
        assert "owner_user_id" in columns
        row = client.app.state.db.query_one(
            "SELECT owner_user_id FROM novelai_upstreams WHERE id = 'default'"
        )
        assert row["owner_user_id"] is None


def test_fresh_db_has_owner_user_id_column(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
admin:
  username: admin
  password: admin123
server:
  host: 127.0.0.1
  port: 8080
queue:
  max_queue_size: 2
database:
  path: "{(tmp_path / "fresh.db").as_posix()}"
logging:
  level: INFO
  directory: "{(tmp_path / "logs").as_posix()}"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        columns = [
            row["name"]
            for row in client.app.state.db.query_all("PRAGMA table_info(novelai_upstreams)")
        ]
        assert "owner_user_id" in columns


def test_admin_upstreams_show_owner_and_render_states(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        db = client.app.state.db
        repo = client.app.state.upstream_runtime.repository
        db.execute("INSERT INTO users(api_key_hash, name, created_at) VALUES ('h-alice', 'Alice', datetime('now'))")
        alice_id = int(db.query_one("SELECT id FROM users WHERE api_key_hash = 'h-alice'")["id"])
        repo.create(upstream_id=f"u{alice_id}-main", api_key="tok-alice", owner_user_id=alice_id)
        db.execute("UPDATE users SET deleted_at = datetime('now') WHERE id = ?", (alice_id,))
        db.execute("INSERT INTO users(api_key_hash, name, created_at) VALUES ('h-bob', 'Bob', datetime('now'))")
        bob_id = int(db.query_one("SELECT id FROM users WHERE api_key_hash = 'h-bob'")["id"])
        repo.create(upstream_id=f"u{bob_id}-bob", api_key="tok-bob", owner_user_id=bob_id)

        resp = client.get("/admin/api/upstreams", auth=("admin", "admin123"))
        assert resp.status_code == 200
        by_id = {upstream["id"]: upstream for upstream in resp.json()["upstreams"]}
        assert by_id["default"]["owner_user_id"] is None
        assert by_id["default"]["owner_user"] is None
        assert by_id[f"u{alice_id}-main"]["owner_user"]["name"] == "Alice"
        assert by_id[f"u{alice_id}-main"]["owner_user"]["deleted_at"] is not None
        assert by_id[f"u{bob_id}-bob"]["owner_user"]["name"] == "Bob"
        assert by_id[f"u{bob_id}-bob"]["owner_user"]["deleted_at"] is None

        # 悬空归属（防御性兜底）：关闭外键后直接删用户行，owner_user_id 保持悬挂。
        db.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            db.execute("DELETE FROM users WHERE id = ?", (bob_id,))
        finally:
            db.conn.execute("PRAGMA foreign_keys = ON")

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        page = client.get("/admin/upstreams")
        assert page.status_code == 200
        text = page.text
        assert "Alice（已删除）" in text
        assert f"#{alice_id}" in text
        assert "未知用户" in text and f"#{bob_id}" in text
        assert "管理员" in text


def test_admin_can_patch_and_delete_self_service_upstream(tmp_path: Path, monkeypatch):
    config_path, _ = _write_self_service_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    from app.main import app

    with TestClient(app) as client:
        _login(client, user_id=DISCORD_USER_ID, username="u1")
        upstream = _create_upstream(client, label="main", api_key="secret-token-a")

        patch = client.patch(
            f"/admin/api/upstreams/{upstream['id']}",
            auth=("admin", "admin123"),
            json={"api_key": "admin-changed"},
        )
        assert patch.status_code == 200
        assert patch.json()["upstream"]["owner_user_id"] is not None

        delete = client.delete(
            f"/admin/api/upstreams/{upstream['id']}",
            auth=("admin", "admin123"),
        )
        assert delete.status_code == 200
        assert client.app.state.db.query_one(
            "SELECT 1 FROM novelai_upstreams WHERE id = ?", (upstream["id"],)
        ) is None
