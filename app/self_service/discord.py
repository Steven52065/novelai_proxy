from __future__ import annotations

from urllib.parse import urlencode

import httpx

from ..discord_validation import parse_discord_snowflake


DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_ME_URL = "https://discord.com/api/users/@me"
DISCORD_GUILDS_URL = "https://discord.com/api/users/@me/guilds"
DISCORD_GUILD_MEMBER_URL = "https://discord.com/api/users/@me/guilds/{guild_id}/member"


class DiscordMemberNotFound(Exception):
    """Discord 明确回报用户不是该服务器成员（404），区别于不确定的传输/解析失败。"""


class DiscordOAuthClient:
    def __init__(self, *, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret

    def authorization_url(self, *, redirect_uri: str, state: str, scope: str = "identify guilds") -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": scope,
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

    async def fetch_guild_member(self, *, access_token: str, guild_id: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                DISCORD_GUILD_MEMBER_URL.format(guild_id=guild_id),
                headers=_auth_headers(access_token),
            )
            # 404 表示用户确实不是该服务器成员，必须先于 raise_for_status 拦截，
            # 否则会退化成“情况不明”的 502，无法据此停用账号。
            if response.status_code == 404:
                raise DiscordMemberNotFound()
            response.raise_for_status()
            payload = response.json()
            discord_member_role_ids(payload)
            return payload


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def discord_oauth_scope(config) -> str:
    # 只申请当前验证策略必需的最小权限：
    # guilds.members.read 只暴露指定服务器的成员信息，范围严格小于暴露全部服务器列表的 guilds。
    if config.require_role:
        return "identify guilds.members.read"
    if config.require_guild:
        return "identify guilds"
    return "identify"


def discord_guild_ids(payload: object) -> set[str]:
    if not isinstance(payload, list):
        raise TypeError("Discord guilds response is not a JSON array")

    guild_ids: set[str] = set()
    for index, guild in enumerate(payload):
        if not isinstance(guild, dict):
            raise TypeError("Discord guilds response contains a non-object item")
        guild_id = parse_discord_snowflake(guild.get("id"), field=f"guilds[{index}].id")
        guild_ids.add(guild_id)
    return guild_ids


def discord_member_role_ids(payload: object) -> set[str]:
    if not isinstance(payload, dict):
        raise TypeError("Discord guild member response is not a JSON object")

    # 正常成员对象必定带 roles 数组（无身份组时为空数组），缺失说明响应不可信，
    # 按格式异常处理而不是当成“没有任何身份组”。
    roles = payload.get("roles")
    if not isinstance(roles, list):
        raise TypeError("Discord guild member response has no roles array")

    role_ids: set[str] = set()
    for index, role_id in enumerate(roles):
        role_ids.add(parse_discord_snowflake(role_id, field=f"member.roles[{index}]"))
    return role_ids
