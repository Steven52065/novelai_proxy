from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from fastapi.testclient import TestClient

from app.csrf import ADMIN_CSRF_COOKIE
from app.signed_tokens import sign_payload, verify_payload
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
