from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..discord_validation import parse_discord_snowflake

if TYPE_CHECKING:
    from .connection import Database


def validate_discord_self_service_config(db: Database, config: Any) -> None:
    discord = config.self_service.discord
    if not discord.enabled:
        return

    missing_fields = [
        field_name
        for field_name in ("client_id", "client_secret", "redirect_uri", "required_guild_id", "session_secret")
        if not str(getattr(discord, field_name, "") or "").strip()
    ]
    if discord.default_group_id is None:
        missing_fields.append("default_group_id")
    if missing_fields:
        formatted = ", ".join(f"self_service.discord.{field_name}" for field_name in missing_fields)
        raise ValueError(f"Discord self-service is enabled but missing required configuration: {formatted}")

    try:
        parse_discord_snowflake(
            discord.required_guild_id,
            field="self_service.discord.required_guild_id",
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "self_service.discord.required_guild_id must be a valid Discord guild snowflake"
        ) from exc

    row = db.query_one(
        "SELECT id, is_active FROM user_groups WHERE id = ?",
        (discord.default_group_id,),
    )
    if row is None:
        raise ValueError(
            "self_service.discord.default_group_id must reference an existing enabled user_groups.id "
            f"(got {discord.default_group_id})"
        )
    if not int(row["is_active"]):
        raise ValueError(
            "self_service.discord.default_group_id must reference an enabled user group "
            f"(got disabled group {discord.default_group_id})"
        )
