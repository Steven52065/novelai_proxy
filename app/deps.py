from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, Request

if TYPE_CHECKING:
    from .config import AppConfig, DiscordSelfServiceConfig
    from .database import Database
    from .free_small_daily_limit import FreeSmallDailyLimitManager
    from .proxy.service import ProxyRequestService
    from .routing_queue import RoutingProxyQueue
    from .quota_manager import QuotaManager
    from .self_service.discord import DiscordOAuthClient


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_quota_manager(request: Request) -> QuotaManager:
    return request.app.state.quota_manager


def get_free_small_daily_limit_manager(request: Request) -> FreeSmallDailyLimitManager:
    return request.app.state.free_small_daily_limit_manager


def get_proxy_queue(request: Request) -> RoutingProxyQueue:
    return request.app.state.proxy_queue


def get_proxy_service(request: Request) -> ProxyRequestService:
    return request.app.state.proxy_service


def get_discord_oauth_client(request: Request) -> DiscordOAuthClient:
    return request.app.state.discord_oauth_client


def get_discord_self_service_config(
    config: AppConfig = Depends(get_config),
) -> DiscordSelfServiceConfig:
    return config.self_service.discord
