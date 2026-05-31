from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from helpers import write_test_config


def test_health_admin_create_user_and_subscription(tmp_path: Path, monkeypatch):
    config_path = write_test_config(tmp_path)
    monkeypatch.setenv("NOVELAI_PROXY_CONFIG", str(config_path))
    try:
        from app.main import app

        with TestClient(app) as client:
            assert client.get("/health").json()["status"] == "ok"

            create_resp = client.post(
                "/admin/api/users",
                auth=("admin", "admin123"),
                json={"name": "alice", "tier": "normal", "anlas_total": 100},
            )
            assert create_resp.status_code == 200
            api_key = create_resp.json()["api_key"]
            assert api_key.startswith("nai_proxy_")

            sub_resp = client.get("/user/subscription", headers={"Authorization": f"Bearer {api_key}"})
            assert sub_resp.status_code == 200
            body = sub_resp.json()
            assert body["proxyQuota"]["total"] == 100
            assert body["proxyQuota"]["available"] == 100

        log_text = (tmp_path / "logs" / "novelai_proxy.log").read_text(encoding="utf-8")
        assert "http request completed method=GET path=/health status=200" in log_text
        assert "http request details method=GET path=/health" in log_text
    finally:
        monkeypatch.delenv("NOVELAI_PROXY_CONFIG", raising=False)
