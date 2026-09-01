from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import AppConfig, SelfServiceAccountConfig, configuration_security_warnings


def test_novelai_config_is_rejected():
    with pytest.raises(ValidationError, match="admin database"):
        AppConfig.model_validate({"novelai": {"api_key": "pst-secret-token", "account_tier": 3}})


def test_self_service_config_defaults_to_disabled():
    config = AppConfig()

    assert config.self_service.discord.enabled is False
    assert config.self_service.discord.client_id == ""
    assert config.self_service.discord.require_guild is True
    assert config.self_service.discord.require_role is False
    assert config.self_service.discord.required_role_ids == []
    assert config.self_service.discord.disable_new_registration is False
    assert config.self_service.discord.default_group_id is None
    assert config.free_small_daily_limit.reset_hour_utc8 == 0
    assert config.image_hosting.local_format_conversion is False
    assert config.image_hosting.local_conversion_format == "webp"
    assert config.image_hosting.provider == "catbox"
    assert config.image_hosting.sda1.api_url == "https://p.sda1.dev/api/v1/upload_external_noform"
    assert config.database.hot_payload.enabled is False
    assert config.database.hot_payload.compression == "zlib"
    assert config.database.hot_payload.compression_level == 6
    assert config.database.hot_payload.min_bytes == 4096
    assert config.database.hot_payload.min_savings_ratio == 0.10
    assert config.database.auto_vacuum.enabled is True
    assert config.database.auto_vacuum.run_time_utc8 == "04:00"
    assert config.security.secure_cookies == "auto"
    assert config.security.trusted_proxy_ips == ["127.0.0.1", "::1"]


def test_discord_self_service_disable_new_registration_parses_explicit_value():
    config = AppConfig.model_validate(
        {"self_service": {"discord": {"disable_new_registration": True}}}
    )

    assert config.self_service.discord.disable_new_registration is True


def test_logging_level_accepts_warning():
    config = AppConfig.model_validate({"logging": {"level": "WARNING"}})

    assert config.logging.level == "WARNING"


def test_image_hosting_accepts_sda1_provider():
    config = AppConfig.model_validate({"image_hosting": {"enabled": True, "provider": "sda1"}})

    assert config.image_hosting.provider == "sda1"


def test_legacy_log_level_warning_updates_logging_level():
    config = AppConfig.model_validate({"log_level": "WARNING"})

    assert config.log_level == "WARNING"
    assert config.logging.level == "WARNING"


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


def test_configuration_security_warnings_keep_weak_config_compatible():
    config = AppConfig()

    assert configuration_security_warnings(config, config_exists=False) == (
        "configuration file is missing; built-in defaults are active",
        "admin password uses a known development default",
    )


def test_configuration_security_warnings_accept_strong_password():
    config = AppConfig.model_validate({"admin": {"password": "a-long-production-password"}})

    assert configuration_security_warnings(config, config_exists=True) == ()


def test_self_service_account_last_call_days_default_and_validation():
    assert SelfServiceAccountConfig().last_call_days == 7

    config = AppConfig.model_validate({'self_service': {'account': {'last_call_days': 3}}})
    assert config.self_service.account.last_call_days == 3

    with pytest.raises(ValidationError):
        AppConfig.model_validate({'self_service': {'account': {'last_call_days': -1}}})

    with pytest.raises(ValidationError):
        AppConfig.model_validate({'self_service': {'account': {'last_call_days': 3651}}})
