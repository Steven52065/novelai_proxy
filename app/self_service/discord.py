from __future__ import annotations

from urllib.parse import urlencode

import httpx


DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_ME_URL = "https://discord.com/api/users/@me"
DISCORD_GUILDS_URL = "https://discord.com/api/users/@me/guilds"


class DiscordOAuthClient:
    def __init__(self, *, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "identify guilds",
                "state": state,
            }
        )
        return f"{DISCORD_AUTHORIZE_URL}?{query}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                DISCORD_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            return response.json()

    async def fetch_user(self, *, access_token: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(DISCORD_ME_URL, headers=_auth_headers(access_token))
            response.raise_for_status()
            return response.json()

    async def fetch_guilds(self, *, access_token: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(DISCORD_GUILDS_URL, headers=_auth_headers(access_token))
            response.raise_for_status()
            payload = response.json()
            discord_guild_ids(payload)
            return payload


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def discord_guild_ids(payload: object) -> set[str]:
    if not isinstance(payload, list):
        raise TypeError("Discord guilds response is not a JSON array")

    guild_ids: set[str] = set()
    for guild in payload:
        if not isinstance(guild, dict):
            raise TypeError("Discord guilds response contains a non-object item")
        guild_id = str(guild.get("id") or "").strip()
        if not guild_id:
            raise ValueError("Discord guilds response contains an item without an id")
        guild_ids.add(guild_id)
    return guild_ids
