from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _write_config(tmp_path: Path, cors_yaml: str) -> Path:
    db_path = tmp_path / "test.db"
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
  upstream_interval_min_seconds: 0
  upstream_interval_max_seconds: 0
  upstream_error_extra_delay_seconds: 0
  upstream_execution_timeout_seconds: 60
novelai:
  api_key: ""
  account_tier: 3
database:
  path: "{db_path.as_posix()}"
logging:
  level: DEBUG
  directory: "{(tmp_path / "logs").as_posix()}"
cors:
{cors_yaml}
""",
        encoding="utf-8",
    )
    return config_path


def test_cors_disabled_does_not_handle_preflight_or_add_headers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "NOVELAI_PROXY_CONFIG",
        str(
            _write_config(
                tmp_path,
                """
  enabled: false
  allow_origins:
    - "https://client.example"
""",
            )
        ),
    )
    from app.main import app

    with TestClient(app) as client:
        preflight = client.options(
            "/ai/generate-image",
            headers={
                "Origin": "https://client.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        simple = client.get("/health", headers={"Origin": "https://client.example"})

        assert preflight.status_code == 405
        assert "access-control-allow-origin" not in preflight.headers
        assert simple.status_code == 200
        assert "access-control-allow-origin" not in simple.headers


def test_cors_regex_origin_credentials_and_max_age(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "NOVELAI_PROXY_CONFIG",
        str(
            _write_config(
                tmp_path,
                """
  enabled: true
  allow_origins: []
  allow_origin_regex: "https://.*[.]example[.]com"
  allow_methods:
    - POST
  allow_headers:
    - authorization
    - content-type
  expose_headers:
    - Content-Disposition
  allow_credentials: true
  max_age: 123
""",
            )
        ),
    )
    from app.main import app

    with TestClient(app) as client:
        preflight = client.options(
            "/ai/generate-image",
            headers={
                "Origin": "https://client.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        simple = client.get("/health", headers={"Origin": "https://client.example.com"})

        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "https://client.example.com"
        assert preflight.headers["access-control-allow-credentials"] == "true"
        assert preflight.headers["access-control-max-age"] == "123"
        assert "authorization" in preflight.headers["access-control-allow-headers"].lower()
        assert simple.headers["access-control-allow-origin"] == "https://client.example.com"
        assert simple.headers["access-control-expose-headers"] == "Content-Disposition"


def test_cors_disallowed_origin_preflight_is_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "NOVELAI_PROXY_CONFIG",
        str(
            _write_config(
                tmp_path,
                """
  enabled: true
  allow_origins:
    - "https://client.example"
  allow_methods:
    - POST
  allow_headers:
    - authorization
""",
            )
        ),
    )
    from app.main import app

    with TestClient(app) as client:
        preflight = client.options(
            "/ai/generate-image",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )

        assert preflight.status_code == 400
        assert "access-control-allow-origin" not in preflight.headers
