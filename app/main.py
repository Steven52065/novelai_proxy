from __future__ import annotations

import sys
import asyncio
from contextlib import asynccontextmanager
from os import environ
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
sdk_src = repo_root / "novelai-python" / "src"
if sdk_src.exists() and str(sdk_src) not in sys.path:
    sys.path.insert(0, str(sdk_src))

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .admin.routes import (
    AdminLoginRequired,
    admin_login_required_handler,
    router as admin_router,
)
from .admin.session_middleware import AdminSessionRefreshMiddleware
from .config import load_config
from .cors import ConfigurableCORSMiddleware
from .dashboard_events import DashboardEventBus
from .database import Database, validate_discord_self_service_config
from .domain_errors import DomainError
from .free_small_daily_limit import FreeSmallDailyLimitManager
from .image_hosts import ImageHostingService
from .logging_utils import RequestLoggingMiddleware, configure_logging, json_dumps, logger
from .payload_archive import PayloadArchiveService
from .proxy.routes import router as proxy_router
from .proxy.service import ProxyRequestService
from .queue_manager import RoutingProxyQueue, UpstreamQueueTarget
from .quota_manager import QuotaManager
from .rate_limiter import RateLimiter
from .self_service.discord import DiscordOAuthClient
from .self_service.routes import router as self_service_router
from .upstream import UpstreamClient
from .users.service import user_is_available
from .usage_logs import UsageLogRepository


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config(environ.get("NOVELAI_PROXY_CONFIG", "config.yaml"))
    configure_logging(config.logging)
    db = Database(config.database.path)
    db.init_schema()
    validate_discord_self_service_config(db, config)
    dashboard_events = DashboardEventBus()
    quota_manager = QuotaManager(db)
    free_small_daily_limit_manager = FreeSmallDailyLimitManager(
        db,
        reset_hour_utc8=config.free_small_daily_limit.reset_hour_utc8,
    )
    usage_logs = UsageLogRepository(db, on_change=dashboard_events.notify_nowait)
    payload_archive_service = PayloadArchiveService(db)
    upstream_clients = _build_upstream_clients(config)
    default_upstream_id = next(iter(upstream_clients))
    upstream = upstream_clients[default_upstream_id]
    proxy_queue = RoutingProxyQueue(
        targets=[
            UpstreamQueueTarget(
                id=upstream_id,
                client_provider=lambda upstream_id=upstream_id: app.state.upstream
                if upstream_id == app.state.default_upstream_id
                else app.state.upstream_clients[upstream_id],
            )
            for upstream_id in upstream_clients
        ],
        quota_manager=quota_manager,
        usage_logs=usage_logs,
        max_queue_size=config.queue.max_queue_size,
        dispatch_max_queue_size=config.queue.dispatch_max_queue_size,
        routing_strategy=config.routing.strategy,
        adaptive_initial_score=config.routing.adaptive.initial_score,
        adaptive_alpha=config.routing.adaptive.alpha,
        adaptive_min_weight=config.routing.adaptive.min_weight,
        upstream_interval_min_seconds=config.queue.upstream_interval_min_seconds,
        upstream_interval_max_seconds=config.queue.upstream_interval_max_seconds,
        upstream_error_extra_delay_seconds=config.queue.upstream_error_extra_delay_seconds,
        upstream_execution_timeout_seconds=config.queue.upstream_execution_timeout_seconds,
        retry_429_queue_length_threshold=config.queue.retry_429_queue_length_threshold,
        retry_429_max_attempts=config.queue.retry_429_max_attempts,
        is_user_available=lambda user_id: user_is_available(db, user_id),
        image_hosting=ImageHostingService(config.image_hosting),
        on_change=dashboard_events.notify_nowait,
    )

    app.state.config = config
    app.state.dashboard_events = dashboard_events
    app.state.db = db
    app.state.quota_manager = quota_manager
    app.state.free_small_daily_limit_manager = free_small_daily_limit_manager
    app.state.usage_logs = usage_logs
    app.state.payload_archive_service = payload_archive_service
    app.state.rate_limiter = RateLimiter(db)
    app.state.upstream = upstream
    app.state.upstream_clients = upstream_clients
    app.state.default_upstream_id = default_upstream_id
    app.state.discord_oauth_client = DiscordOAuthClient(
        client_id=config.self_service.discord.client_id,
        client_secret=config.self_service.discord.client_secret,
    )
    app.state.proxy_queue = proxy_queue
    app.state.proxy_service = ProxyRequestService(
        rate_limiter=app.state.rate_limiter,
        quota_manager=quota_manager,
        free_small_daily_limit_manager=free_small_daily_limit_manager,
        proxy_queue=proxy_queue,
        usage_logs=usage_logs,
        logging_config=config.logging,
    )

    proxy_queue.start()
    payload_archive_task = None
    if config.database.payload_archive.enabled:
        payload_archive_task = asyncio.create_task(_payload_archive_loop(payload_archive_service, config.database.payload_archive))
    try:
        yield
    finally:
        if payload_archive_task is not None:
            payload_archive_task.cancel()
            try:
                await payload_archive_task
            except asyncio.CancelledError:
                pass
        await proxy_queue.stop()
        db.close()


def _build_upstream_clients(config) -> dict[str, UpstreamClient]:
    enabled_upstreams = [upstream for upstream in config.novelai.upstreams if upstream.enabled]
    if enabled_upstreams:
        return {
            upstream.id: UpstreamClient(upstream.api_key)
            for upstream in enabled_upstreams
        }
    return {"default": UpstreamClient(config.novelai.api_key)}


async def _payload_archive_loop(service: PayloadArchiveService, config) -> None:
    interval_seconds = max(float(config.run_interval_hours), 0.01) * 3600
    while True:
        try:
            result = await asyncio.to_thread(
                service.archive_due_payloads,
                hot_days=config.hot_days,
                compression=config.compression,
                compression_level=config.compression_level,
                max_payloads_per_part=config.max_payloads_per_part,
                max_raw_bytes_per_part=config.max_raw_bytes_per_part,
                max_rows_per_run=config.max_rows_per_run,
            )
            if result["archived_payloads"]:
                logger.info(
                    "payload archive completed payloads=%s parts=%s raw_bytes=%s compressed_bytes=%s",
                    result["archived_payloads"],
                    result["archived_parts"],
                    result["raw_bytes"],
                    result["compressed_bytes"],
                )
        except Exception:
            logger.exception("payload archive task failed")
        await asyncio.sleep(interval_seconds)


app = FastAPI(title="NovelAI Proxy", lifespan=lifespan)
app.add_middleware(ConfigurableCORSMiddleware)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(AdminSessionRefreshMiddleware)
static_dir = repo_root / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.include_router(proxy_router)
app.include_router(admin_router)
app.include_router(self_service_router)
# 管理后台页面的会话依赖未通过时，统一重定向到登录页。
app.add_exception_handler(AdminLoginRequired, admin_login_required_handler)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    logger.warning("http exception path=%s status=%s detail=%s", request.url.path, exc.status_code, exc.detail)
    content = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError):
    logger.warning("domain error path=%s status=%s message=%s", request.url.path, exc.status_code, exc.message)
    return JSONResponse(status_code=exc.status_code, content={"message": exc.message})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.error("request validation failed path=%s errors=%s", request.url.path, json_dumps(exc.errors()))
    return JSONResponse(status_code=400, content={"message": "Invalid request", "details": exc.errors()})
