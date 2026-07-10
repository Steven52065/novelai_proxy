from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from fastapi.testclient import TestClient

from fastapi import Request

from app.admin.auth import SESSION_COOKIE, SESSION_COOKIE_MAX_AGE_SECONDS, has_admin_session
from app.signed_tokens import sign_payload, verify_payload
from helpers import csrf_form, write_test_config


def test_admin_api_rejects_missing_and_wrong_basic_auth(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        missing = client.get("/admin/api/users")
        wrong = client.get("/admin/api/users", auth=("admin", "wrong-password"))

        assert missing.status_code == 401
        assert wrong.status_code == 401
        assert wrong.json()["message"] == "Invalid admin credentials"


def test_proxy_api_key_cannot_access_admin_api(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        create_resp = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "not-admin", "tier": "normal", "anlas_total": 100},
        )
        api_key = create_resp.json()["api_key"]

        resp = client.get("/admin/api/users", headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code in {401, 403}


def test_admin_json_apis_accept_session_cookie(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200

        for path in (
            "/admin/api/users",
            "/admin/api/user-groups",
            "/admin/api/upstreams",
            "/admin/api/database/stats",
            "/admin/api/notifications/pending",
        ):
            assert client.get(path).status_code == 200


def test_admin_web_pages_redirect_without_session_and_after_logout(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        without_session = client.get("/admin/users", follow_redirects=False)
        assert without_session.status_code == 303
        assert without_session.headers["location"] == "/admin/login"
        without_group_session = client.get("/admin/user-groups", follow_redirects=False)
        assert without_group_session.status_code == 303
        assert without_group_session.headers["location"] == "/admin/login"

        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        assert client.get("/admin/users").status_code == 200
        assert client.get("/admin/user-groups").status_code == 200

        logout = client.post("/admin/logout", data=csrf_form(client), follow_redirects=False)
        assert logout.status_code == 303
        assert logout.headers["location"] == "/admin/login"
        after_logout = client.get("/admin/users", follow_redirects=False)
        assert after_logout.status_code == 303
        assert after_logout.headers["location"] == "/admin/login"
        after_group_logout = client.get("/admin/user-groups", follow_redirects=False)
        assert after_group_logout.status_code == 303
        assert after_group_logout.headers["location"] == "/admin/login"


def test_admin_login_failure_does_not_set_session_cookie(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        resp = client.post("/admin/login", data={"username": "admin", "password": "wrong"})

        assert resp.status_code == 401
        assert "novelai_proxy_admin" not in resp.headers.get("set-cookie", "")


def test_admin_session_rejects_legacy_expired_and_tampered_tokens(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    secret = _admin_session_secret("admin123")
    expired = sign_payload({"exp": 1, "sub": "admin"}, secret)
    valid = sign_payload({"exp": 4_102_444_800, "sub": "admin"}, secret)
    tampered = f"{valid[:-1]}{'a' if valid[-1] != 'a' else 'b'}"

    with TestClient(app) as client:
        for token in ("admin:legacy-signature", expired, tampered):
            client.cookies.set(SESSION_COOKIE, token)
            resp = client.get("/admin/users", follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers["location"] == "/admin/login"


def test_admin_session_rejects_non_ascii_signature_cookie(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/admin/users",
                "headers": [(b"cookie", b"novelai_proxy_admin=abc.\xe9")],
                "app": app,
                "server": ("testserver", 80),
                "scheme": "http",
                "client": ("testclient", 50000),
                "query_string": b"",
            }
        )

        assert has_admin_session(request) is False


def test_signed_token_rejects_non_ascii_body_without_error():
    assert verify_payload("\u00e9.signature", "secret") is None


def test_signed_token_rejects_non_ascii_signature_without_error():
    assert verify_payload("abc.\u00e9", "secret") is None


def test_admin_session_refresh_extends_expiration(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    import app.signed_tokens as signed_tokens
    from app.main import app

    now = 1_700_000_000
    monkeypatch.setattr(signed_tokens.time, "time", lambda: now)
    secret = _admin_session_secret("admin123")

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"}, follow_redirects=False)
        assert login.status_code == 303
        first_payload = verify_payload(client.cookies.get(SESSION_COOKIE), secret)
        assert first_payload["exp"] == now + SESSION_COOKIE_MAX_AGE_SECONDS

        now += 60
        resp = client.get("/admin/users")
        assert resp.status_code == 200
        refreshed_payload = verify_payload(client.cookies.get(SESSION_COOKIE), secret)
        assert refreshed_payload["exp"] == now + SESSION_COOKIE_MAX_AGE_SECONDS
        assert refreshed_payload["exp"] > first_payload["exp"]


def test_admin_session_cookie_sets_secure_on_https(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app, base_url="https://testserver") as client:
        resp = client.post(
            "/admin/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert "secure" in resp.headers["set-cookie"].lower()


def test_admin_session_cookie_auto_trusts_forwarded_https_from_configured_proxy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        client.app.state.config.security.trusted_proxy_ips = ["testclient"]
        resp = client.post(
            "/admin/login",
            headers={"X-Forwarded-Proto": "https"},
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert "secure" in resp.headers["set-cookie"].lower()


def test_admin_session_cookie_modes_override_request_scheme(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app, base_url="https://testserver") as client:
        client.app.state.config.security.secure_cookies = "never"
        never = client.post(
            "/admin/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )
        assert "secure" not in never.headers["set-cookie"].lower()

        client.app.state.config.security.secure_cookies = "always"
        always = client.post(
            "/admin/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )
        assert "secure" in always.headers["set-cookie"].lower()


def _admin_session_secret(password: str) -> str:
    return hmac.new(password.encode("utf-8"), b"novelai-proxy-admin-session-v1", hashlib.sha256).hexdigest()
