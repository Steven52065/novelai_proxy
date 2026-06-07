from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from helpers import write_test_config


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

        logout = client.post("/admin/logout", follow_redirects=False)
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
