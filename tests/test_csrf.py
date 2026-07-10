from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from fastapi.testclient import TestClient

from app.csrf import ADMIN_CSRF_COOKIE, SELF_SERVICE_CSRF_COOKIE
from app.signed_tokens import expiring_payload, sign_payload, verify_payload
from helpers import FakeUpstream, PAYLOAD, csrf_headers, write_test_config


def test_admin_cookie_mutations_require_valid_csrf_but_basic_remains_compatible(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/admin/login", data={"username": "admin", "password": "admin123"})
        assert login.status_code == 200
        token = client.cookies.get(ADMIN_CSRF_COOKIE)
        assert token

        missing = client.post("/admin/api/users", json={"name": "missing-csrf"})
        assert missing.status_code == 403
        assert "refresh" in missing.json()["message"]
        assert missing.headers["cache-control"] == "no-store, private"
        assert missing.headers["pragma"] == "no-cache"

        tampered = client.post(
            "/admin/api/users",
            headers={"X-CSRF-Token": f"{token}x"},
            json={"name": "tampered-csrf"},
        )
        assert tampered.status_code == 403

        valid = client.post(
            "/admin/api/users",
            headers=csrf_headers(client),
            json={"name": "valid-csrf", "tier": "normal", "anlas_total": 10},
        )
        assert valid.status_code == 200

        basic = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "basic-compatible", "tier": "normal", "anlas_total": 10},
        )
        assert basic.status_code == 200


def test_invalid_basic_does_not_bypass_admin_session_csrf(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        assert client.post("/admin/login", data={"username": "admin", "password": "admin123"}).status_code == 200

        response = client.post(
            "/admin/api/users",
            auth=("wrong", "wrong"),
            json={"name": "invalid-basic-csrf-bypass"},
        )

        assert response.status_code == 403
        assert "CSRF" in response.json()["message"]


def test_basic_admin_mutations_reject_cross_site_browser_requests(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    cross_site_headers = (
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "https://attacker.example"},
        {"Referer": "https://attacker.example/form"},
        {"Origin": "null"},
    )
    with TestClient(app) as client:
        for index, headers in enumerate(cross_site_headers):
            response = client.post(
                "/admin/api/users",
                auth=("admin", "admin123"),
                headers=headers,
                json={"name": f"cross-site-basic-{index}"},
            )

            assert response.status_code == 403
            assert response.json()["message"] == "Cross-site Basic-authenticated admin request is not allowed"
            assert response.headers["cache-control"] == "no-store, private"

        count = client.app.state.db.query_one("SELECT COUNT(*) AS c FROM users")["c"]
        assert count == 0


def test_basic_admin_mutations_allow_same_origin_browser_and_script_clients(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        script = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "basic-script-client"},
        )
        same_origin = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
            json={"name": "basic-same-origin-browser"},
        )

        assert script.status_code == 200
        assert same_origin.status_code == 200


def test_admin_csrf_token_cannot_be_reused_after_new_login(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        assert client.post("/admin/login", data={"username": "admin", "password": "admin123"}).status_code == 200
        old_token = client.cookies.get(ADMIN_CSRF_COOKIE)
        assert client.post("/admin/logout", headers=csrf_headers(client), follow_redirects=False).status_code == 303

        assert client.post("/admin/login", data={"username": "admin", "password": "admin123"}).status_code == 200
        client.cookies.set(ADMIN_CSRF_COOKIE, old_token)
        reused = client.post(
            "/admin/api/users",
            headers={"X-CSRF-Token": old_token},
            json={"name": "cross-session"},
        )
        assert reused.status_code == 403


def test_admin_csrf_rejects_expired_signed_token(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        assert client.post("/admin/login", data={"username": "admin", "password": "admin123"}).status_code == 200
        admin_secret = hmac.new(
            b"admin123",
            b"novelai-proxy-admin-session-v1",
            hashlib.sha256,
        ).hexdigest()
        session = verify_payload(client.cookies.get("novelai_proxy_admin"), admin_secret)
        csrf_secret = hashlib.sha256(f"{admin_secret}:csrf:v1".encode("utf-8")).hexdigest()
        expired = sign_payload(
            {"exp": 1, "scope": "admin", "sid": session["sid"], "nonce": "expired"},
            csrf_secret,
        )
        client.cookies.set(ADMIN_CSRF_COOKIE, expired)

        response = client.post(
            "/admin/api/users",
            headers={"X-CSRF-Token": expired},
            json={"name": "expired-csrf"},
        )
        assert response.status_code == 403


def test_legacy_admin_session_is_cleared_and_requires_login(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    admin_secret = hmac.new(
        b"admin123",
        b"novelai-proxy-admin-session-v1",
        hashlib.sha256,
    ).hexdigest()
    legacy_session = sign_payload(expiring_payload(3600, sub="admin"), admin_secret)

    with TestClient(app) as client:
        response = client.get(
            "/admin",
            headers={
                "Cookie": (
                    f"novelai_proxy_admin={legacy_session}; "
                    f"{ADMIN_CSRF_COOKIE}=stale-csrf"
                )
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"
        assert response.headers["cache-control"] == "no-store, private"
        assert response.headers["pragma"] == "no-cache"
        cleared = response.headers.get_list("set-cookie")
        assert any("novelai_proxy_admin=" in value and "Max-Age=0" in value for value in cleared)
        assert any(f"{ADMIN_CSRF_COOKIE}=" in value and "Max-Age=0" in value for value in cleared)
        assert not any(
            value.startswith("novelai_proxy_admin=") and "Max-Age=0" not in value
            for value in cleared
        )
        assert client.cookies.get("novelai_proxy_admin") is None

        next_request = client.get("/admin", follow_redirects=False)
        assert next_request.status_code == 303
        assert next_request.headers["location"] == "/admin/login"


def test_legacy_admin_session_does_not_override_valid_basic_auth(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    admin_secret = hmac.new(
        b"admin123",
        b"novelai-proxy-admin-session-v1",
        hashlib.sha256,
    ).hexdigest()
    legacy_session = sign_payload(expiring_payload(3600, sub="admin"), admin_secret)

    with TestClient(app) as client:
        response = client.get(
            "/admin/api/users",
            auth=("admin", "admin123"),
            headers={"Cookie": f"novelai_proxy_admin={legacy_session}"},
            follow_redirects=False,
        )

        assert response.status_code == 200
        assert not any(
            value.startswith("novelai_proxy_admin=")
            for value in response.headers.get_list("set-cookie")
        )
        assert client.cookies.get("novelai_proxy_admin") is None


def test_invalid_basic_does_not_preserve_legacy_admin_session(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    admin_secret = hmac.new(
        b"admin123",
        b"novelai-proxy-admin-session-v1",
        hashlib.sha256,
    ).hexdigest()
    legacy_session = sign_payload(expiring_payload(3600, sub="admin"), admin_secret)

    with TestClient(app) as client:
        client.cookies.set("novelai_proxy_admin", legacy_session, path="/")
        response = client.post(
            "/admin/api/users",
            auth=("wrong", "wrong"),
            json={"name": "legacy-invalid-basic"},
        )

        assert response.status_code == 401
        assert response.json()["login_url"] == "/admin/login"
        assert any(
            "novelai_proxy_admin=" in value and "Max-Age=0" in value
            for value in response.headers.get_list("set-cookie")
        )


def test_legacy_self_service_session_is_cleared_and_requires_login(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    legacy_session = sign_payload(expiring_payload(3600, user_id=1), "")

    with TestClient(app) as client:
        client.cookies.set("novelai_proxy_self_service_session", legacy_session, path="/")
        client.cookies.set(SELF_SERVICE_CSRF_COOKIE, "stale-csrf", path="/")
        response = client.get("/account", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/signup"
        cleared = response.headers.get_list("set-cookie")
        assert any("novelai_proxy_self_service_session=" in value and "Max-Age=0" in value for value in cleared)
        assert any(f"{SELF_SERVICE_CSRF_COOKIE}=" in value and "Max-Age=0" in value for value in cleared)


def test_legacy_session_write_returns_relogin_response(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    admin_secret = hmac.new(
        b"admin123",
        b"novelai-proxy-admin-session-v1",
        hashlib.sha256,
    ).hexdigest()
    legacy_session = sign_payload(expiring_payload(3600, sub="admin"), admin_secret)

    with TestClient(app) as client:
        client.cookies.set("novelai_proxy_admin", legacy_session, path="/")
        response = client.post("/admin/logout", follow_redirects=False)

        assert response.status_code == 401
        assert response.json()["login_url"] == "/admin/login"
        assert any(
            "novelai_proxy_admin=" in value and "Max-Age=0" in value
            for value in response.headers.get_list("set-cookie")
        )


def test_bearer_image_api_does_not_require_csrf(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(write_test_config(tmp_path)))
    from app.main import app

    with TestClient(app) as client:
        app.state.upstream = FakeUpstream()
        created = client.post(
            "/admin/api/users",
            auth=("admin", "admin123"),
            json={"name": "csrf-api-client", "tier": "normal", "anlas_total": 100},
        )
        api_key = created.json()["api_key"]

        response = client.post(
            "/ai/generate-image",
            headers={"Authorization": f"Bearer {api_key}"},
            json=PAYLOAD,
        )
        assert response.status_code == 201
        assert response.headers["content-type"].startswith("application/zip")
