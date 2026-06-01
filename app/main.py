from __future__ import annotations

import sys
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

from .admin.routes import router as admin_router, set_admin_session_cookie, valid_admin_session
from .config import load_config
from .cors import ConfigurableCORSMiddleware
from .database import Database
from .image_hosts import ImageHostingService
from .logging_utils import RequestLoggingMiddleware, configure_logging, json_dumps, logger
from .proxy.routes import router as proxy_router
from .proxy.service import ProxyRequestService
from .queue_manager import RoutingProxyQueue, UpstreamQueueTarget
from .quota_manager import QuotaManager
from .rate_limiter import RateLimiter
from .upstream import UpstreamClient
from .usage_logs import UsageLogRepository


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config(environ.get("NOVELAI_PROXY_CONFIG", "config.yaml"))
    configure_logging(config.logging)
    db = Database(config.database.path)
    db.init_schema()
    quota_manager = QuotaManager(db)
    usage_logs = UsageLogRepository(db)
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
        routing_strategy=config.routing.strategy,
        upstream_interval_min_seconds=config.queue.upstream_interval_min_seconds,
        upstream_interval_max_seconds=config.queue.upstream_interval_max_seconds,
        upstream_error_extra_delay_seconds=config.queue.upstream_error_extra_delay_seconds,
        image_hosting=ImageHostingService(config.image_hosting),
    )

    app.state.config = config
    app.state.db = db
    app.state.quota_manager = quota_manager
    app.state.usage_logs = usage_logs
    app.state.rate_limiter = RateLimiter(db)
    app.state.upstream = upstream
    app.state.upstream_clients = upstream_clients
    app.state.default_upstream_id = default_upstream_id
    app.state.proxy_queue = proxy_queue
    app.state.proxy_service = ProxyRequestService(
        rate_limiter=app.state.rate_limiter,
        quota_manager=quota_manager,
        proxy_queue=proxy_queue,
        usage_logs=usage_logs,
        logging_config=config.logging,
    )

    proxy_queue.start()
    try:
        yield
    finally:
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


app = FastAPI(title="NovelAI Proxy", lifespan=lifespan)
app.add_middleware(ConfigurableCORSMiddleware)
app.add_middleware(RequestLoggingMiddleware)
static_dir = repo_root / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.include_router(proxy_router)
app.include_router(admin_router)


@app.middleware("http")
async def refresh_admin_session_cookie(request: Request, call_next):
    response = await call_next(request)
    if (
        request.url.path.startswith("/admin")
        and request.url.path != "/admin/login"
        and request.url.path != "/admin/logout"
        and valid_admin_session(request)
        and response.status_code < 400
    ):
        set_admin_session_cookie(response, request)
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    logger.warning("http exception path=%s status=%s detail=%s", request.url.path, exc.status_code, exc.detail)
    content = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.error("request validation failed path=%s errors=%s", request.url.path, json_dumps(exc.errors()))
    return JSONResponse(status_code=400, content={"message": "Invalid request", "details": exc.errors()})
