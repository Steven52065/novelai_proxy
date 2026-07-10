from __future__ import annotations

import secrets
import time

from fastapi import Request, Response

from .cookies import set_response_cookie


DEFAULT_TTL_SECONDS = 5 * 60


class ApiKeyFlashStore:
    """新生成 API Key 的一次性闪存：cookie 只存随机令牌，明文 key 留在进程内存并限时过期。

    owner_id 用于把闪存绑定到特定用户（自助账号页防串号）；管理后台场景不传即跳过绑定校验。
    """

    def __init__(self, *, cookie_name: str, state_attr: str, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.cookie_name = cookie_name
        self._state_attr = state_attr
        self._ttl_seconds = ttl_seconds

    def set_flash(self, response: Response, request: Request, api_key: str, *, owner_id: int | None = None) -> None:
        token = secrets.token_urlsafe(24)
        store = self._store(request)
        self._cleanup(store)
        store[token] = (owner_id, api_key, time.monotonic() + self._ttl_seconds)
        set_response_cookie(
            response,
            request,
            self.cookie_name,
            token,
            max_age=self._ttl_seconds,
        )

    def pop_flash(self, request: Request, *, owner_id: int | None = None) -> str | None:
        token = request.cookies.get(self.cookie_name)
        if not token:
            return None
        store = self._store(request)
        self._cleanup(store)
        value = store.pop(token, None)
        if value is None:
            return None
        stored_owner_id, api_key, expires_at = value
        if time.monotonic() > expires_at:
            return None
        if stored_owner_id is not None and stored_owner_id != owner_id:
            return None
        return api_key

    def _store(self, request: Request) -> dict[str, tuple[int | None, str, float]]:
        store = getattr(request.app.state, self._state_attr, None)
        if store is None:
            store = {}
            setattr(request.app.state, self._state_attr, store)
        return store

    def _cleanup(self, store: dict[str, tuple[int | None, str, float]]) -> None:
        now = time.monotonic()
        for token, (_, _, expires_at) in list(store.items()):
            if expires_at <= now:
                store.pop(token, None)
