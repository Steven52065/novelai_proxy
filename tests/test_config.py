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


def test_self_service_config_defaults_to_disabled():
    config = AppConfig()

    assert config.self_service.discord.enabled is False
    assert config.self_service.discord.client_id == ""
    assert config.self_service.discord.default_group_id is None
    assert config.free_small_daily_limit.reset_hour_utc8 == 0


def test_free_small_daily_limit_reset_hour_validation():
    assert AppConfig.model_validate({"free_small_daily_limit": {"reset_hour_utc8": 0}}).free_small_daily_limit.reset_hour_utc8 == 0
    assert AppConfig.model_validate({"free_small_daily_limit": {"reset_hour_utc8": 23}}).free_small_daily_limit.reset_hour_utc8 == 23

    with pytest.raises(ValidationError):
        AppConfig.model_validate({"free_small_daily_limit": {"reset_hour_utc8": 24}})

    with pytest.raises(ValidationError):
        AppConfig.model_validate({"free_small_daily_limit": {"reset_hour_utc8": -1}})


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
