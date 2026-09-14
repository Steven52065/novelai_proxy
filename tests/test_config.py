from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config import (
    AppConfig,
    IdleFreeSmallConfig,
    SelfServiceAccountConfig,
    UpstreamAutoDisableConfig,
    configuration_security_warnings,
)


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


def test_idle_free_small_config_defaults_and_validation():
    config = AppConfig()
    assert config.idle_free_small.occupancy_threshold_percent == 50
    assert config.idle_free_small.min_idle_seconds == 30

    assert IdleFreeSmallConfig(occupancy_threshold_percent=0, min_idle_seconds=0).occupancy_threshold_percent == 0
    assert IdleFreeSmallConfig(occupancy_threshold_percent=100).min_idle_seconds == 30

    with pytest.raises(ValidationError):
        IdleFreeSmallConfig(occupancy_threshold_percent=-0.1)
    with pytest.raises(ValidationError):
        IdleFreeSmallConfig(occupancy_threshold_percent=100.1)
    with pytest.raises(ValidationError):
        IdleFreeSmallConfig(occupancy_threshold_percent=float('nan'))
    with pytest.raises(ValidationError):
        IdleFreeSmallConfig(occupancy_threshold_percent=float('inf'))
    with pytest.raises(ValidationError):
        IdleFreeSmallConfig(min_idle_seconds=-1)
    with pytest.raises(ValidationError):
        IdleFreeSmallConfig(min_idle_seconds=float('inf'))


def test_idle_free_small_config_example_matches_code_defaults():
    example_path = Path(__file__).resolve().parent.parent / 'config.example.yaml'
    example = yaml.safe_load(example_path.read_text(encoding='utf-8'))
    assert example['idle_free_small']['occupancy_threshold_percent'] == IdleFreeSmallConfig().occupancy_threshold_percent
    assert example['idle_free_small']['min_idle_seconds'] == IdleFreeSmallConfig().min_idle_seconds


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


def test_upstream_auto_disable_defaults():
    """默认按账号不可用的 HTTP 状态码或 AuthError 禁用。"""
    config = UpstreamAutoDisableConfig()
    assert config.status_codes == [401, 402, 403]
    assert config.error_types == ["AuthError"]

    app_config = AppConfig()
    assert app_config.upstream_auto_disable.status_codes == [401, 402, 403]
    assert app_config.upstream_auto_disable.error_types == ["AuthError"]


def test_upstream_auto_disable_error_types_are_normalized():
    config = UpstreamAutoDisableConfig(error_types=[" OutOfMemory ", "OutOfMemory", "AuthError"])
    assert config.error_types == ["OutOfMemory", "AuthError"]

    with pytest.raises(ValidationError, match="error_types"):
        UpstreamAutoDisableConfig(error_types=["OutOfMemory", "  "])


def test_config_example_upstream_auto_disable_matches_code_default():
    """模板与代码默认值必须同步。

    两者一旦漂移，照模板部署的实例与省略该段的实例行为就不同，而这段控制的是
    「什么错误会自动禁用共享上游账号」，静默的不一致代价很高。
    """
    example_path = Path(__file__).resolve().parent.parent / "config.example.yaml"
    example = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    defaults = UpstreamAutoDisableConfig()

    assert example["upstream_auto_disable"]["status_codes"] == defaults.status_codes
    assert example["upstream_auto_disable"]["error_types"] == defaults.error_types
