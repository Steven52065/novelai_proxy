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

from .admin.routes import router as admin_router
from .config import load_config
from .database import Database
from .proxy.routes import router as proxy_router
from .queue_manager import ProxyQueue
from .quota_manager import QuotaManager
from .rate_limiter import RateLimiter
from .upstream import UpstreamClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config(environ.get("NOVELAI_PROXY_CONFIG", "config.yaml"))
    db = Database(config.database.path)
    db.init_schema()
    quota_manager = QuotaManager(db)
    upstream = UpstreamClient(config.novelai.api_key)
    proxy_queue = ProxyQueue(
        db=db,
        quota_manager=quota_manager,
        max_queue_size=config.queue.max_queue_size,
    )

    app.state.config = config
    app.state.db = db
    app.state.quota_manager = quota_manager
    app.state.rate_limiter = RateLimiter(db)
    app.state.upstream = upstream
    app.state.proxy_queue = proxy_queue

    proxy_queue.start()
    try:
        yield
    finally:
        await proxy_queue.stop()
        db.close()


app = FastAPI(title="NovelAI Proxy", lifespan=lifespan)
static_dir = repo_root / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.include_router(proxy_router)
app.include_router(admin_router)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    del request
    content = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    del request
    return JSONResponse(status_code=400, content={"message": "Invalid request", "details": exc.errors()})
