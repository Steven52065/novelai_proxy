from __future__ import annotations


MAX_DISCORD_SNOWFLAKE = (1 << 64) - 1


def parse_discord_snowflake(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError(f"{field} must be a decimal snowflake string")

    numeric = int(value)
    if numeric <= 0 or numeric > MAX_DISCORD_SNOWFLAKE:
        raise ValueError(f"{field} is outside the snowflake range")
    if str(numeric) != value:
        raise ValueError(f"{field} must use canonical decimal representation")
    return value
