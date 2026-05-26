from __future__ import annotations

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import CorsConfig


class ConfigurableCORSMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
        self._cors_app: CORSMiddleware | None = None
        self._config_key: tuple | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        config = getattr(scope.get("app").state, "config", None)
        cors_config = getattr(config, "cors", CorsConfig())
        if not cors_config.enabled:
            await self.app(scope, receive, send)
            return

        cors_app = self._get_cors_app(cors_config)
        await cors_app(scope, receive, send)

    def _get_cors_app(self, config: CorsConfig) -> CORSMiddleware:
        key = (
            tuple(config.allow_origins),
            config.allow_origin_regex,
            tuple(config.allow_methods),
            tuple(config.allow_headers),
            tuple(config.expose_headers),
            config.allow_credentials,
            config.max_age,
        )
        if self._cors_app is None or self._config_key != key:
            self._cors_app = CORSMiddleware(
                app=self.app,
                allow_origins=config.allow_origins,
                allow_origin_regex=config.allow_origin_regex,
                allow_methods=config.allow_methods,
                allow_headers=config.allow_headers,
                expose_headers=config.expose_headers,
                allow_credentials=config.allow_credentials,
                max_age=config.max_age,
            )
            self._config_key = key
        return self._cors_app
