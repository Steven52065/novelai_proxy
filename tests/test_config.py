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
