from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import AppConfig


def test_reserved_upstream_id_is_rejected():
    with pytest.raises(ValidationError, match="reserved"):
        AppConfig.model_validate(
            {
                "novelai": {
                    "upstreams": [
                        {"id": "__all__", "api_key": ""},
                    ],
                },
            }
        )


def test_novelai_api_key_config_stays_compatible():
    config = AppConfig.model_validate({"novelai": {"api_key": "pst-secret-token", "account_tier": 3}})

    assert config.novelai.auth_type == "api_key"
    assert config.novelai.api_key == "pst-secret-token"


def test_novelai_login_config_requires_credentials():
    config = AppConfig.model_validate(
        {
            "novelai": {
                "auth_type": "login",
                "username": "user@example.com",
                "password": "secret-password",
            },
        }
    )

    assert config.novelai.auth_type == "login"
    assert config.novelai.username == "user@example.com"
    assert config.novelai.password == "secret-password"


def test_novelai_login_config_rejects_missing_credentials():
    with pytest.raises(ValidationError, match="novelai.username"):
        AppConfig.model_validate({"novelai": {"auth_type": "login", "password": "secret-password"}})

    with pytest.raises(ValidationError, match="novelai.password"):
        AppConfig.model_validate({"novelai": {"auth_type": "login", "username": "user@example.com"}})


def test_novelai_upstreams_allow_mixed_auth_modes():
    config = AppConfig.model_validate(
        {
            "novelai": {
                "upstreams": [
                    {"id": "main", "auth_type": "login", "username": "user@example.com", "password": "secret-password"},
                    {"id": "backup", "auth_type": "api_key", "api_key": "pst-secret-token"},
                ]
            }
        }
    )

    assert [upstream.auth_type for upstream in config.novelai.upstreams] == ["login", "api_key"]
    assert config.novelai.upstreams[0].username == "user@example.com"
    assert config.novelai.upstreams[1].api_key == "pst-secret-token"


def test_novelai_upstream_login_rejects_missing_credentials():
    with pytest.raises(ValidationError, match="novelai.upstreams\\[\\].username"):
        AppConfig.model_validate(
            {
                "novelai": {
                    "upstreams": [
                        {"id": "main", "auth_type": "login", "password": "secret-password"},
                    ]
                }
            }
        )

    with pytest.raises(ValidationError, match="novelai.upstreams\\[\\].password"):
        AppConfig.model_validate(
            {
                "novelai": {
                    "upstreams": [
                        {"id": "main", "auth_type": "login", "username": "user@example.com"},
                    ]
                }
            }
        )


def test_self_service_config_defaults_to_disabled():
    config = AppConfig()

    assert config.self_service.discord.enabled is False
    assert config.self_service.discord.client_id == ""
    assert config.self_service.discord.default_group_id is None
    assert config.free_small_daily_limit.reset_hour_utc8 == 0
    assert config.image_hosting.local_format_conversion is False
    assert config.image_hosting.local_conversion_format == "webp"
    assert config.database.hot_payload.enabled is False
    assert config.database.hot_payload.compression == "zlib"
    assert config.database.hot_payload.compression_level == 6
    assert config.database.hot_payload.min_bytes == 4096
    assert config.database.hot_payload.min_savings_ratio == 0.10
    assert config.database.auto_vacuum.enabled is True
    assert config.database.auto_vacuum.run_time_utc8 == "04:00"


def test_free_small_daily_limit_reset_hour_validation():
    assert AppConfig.model_validate({"free_small_daily_limit": {"reset_hour_utc8": 0}}).free_small_daily_limit.reset_hour_utc8 == 0
    assert AppConfig.model_validate({"free_small_daily_limit": {"reset_hour_utc8": 23}}).free_small_daily_limit.reset_hour_utc8 == 23

    with pytest.raises(ValidationError):
        AppConfig.model_validate({"free_small_daily_limit": {"reset_hour_utc8": 24}})

    with pytest.raises(ValidationError):
        AppConfig.model_validate({"free_small_daily_limit": {"reset_hour_utc8": -1}})


def test_database_auto_vacuum_time_validation():
    config = AppConfig.model_validate({"database": {"auto_vacuum": {"run_time_utc8": "4:05"}}})
    assert config.database.auto_vacuum.run_time_utc8 == "04:05"

    with pytest.raises(ValidationError):
        AppConfig.model_validate({"database": {"auto_vacuum": {"run_time_utc8": "24:00"}}})

    with pytest.raises(ValidationError):
        AppConfig.model_validate({"database": {"auto_vacuum": {"run_time_utc8": "04:60"}}})

    with pytest.raises(ValidationError):
        AppConfig.model_validate({"database": {"auto_vacuum": {"run_time_utc8": "04"}}})


def test_discord_self_service_model_allows_missing_fields_when_disabled():
    config = AppConfig.model_validate(
        {
            "self_service": {
                "discord": {
                    "enabled": False,
                    "client_id": "",
                    "client_secret": "",
                    "redirect_uri": "",
                    "required_guild_id": "",
                    "default_group_id": None,
                    "session_secret": "",
                }
            }
        }
    )

    assert config.self_service.discord.enabled is False
