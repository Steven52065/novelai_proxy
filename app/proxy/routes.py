from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from novelai_python._exceptions import APIError
from novelai_python.sdk.ai.augment_image import AugmentImageInfer
from novelai_python.sdk.ai.generate_image import GenerateImageInfer
from novelai_python.sdk.ai.upscale import Upscale

from ..auth import UserContext, get_current_user
from ..database import Database, utc_now_iso
from ..queue_manager import ProxyQueue, QueueFull
from ..quota_manager import InsufficientQuota, QuotaManager
from ..rate_limiter import RateLimiter


router = APIRouter()


@router.get("/health")
async def health(request: Request):
    return {
        "status": "ok",
        "queue_size": request.app.state.proxy_queue.qsize(),
    }


@router.post("/ai/generate-image")
async def generate_image(
    req: GenerateImageInfer,
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    try:
        estimated_cost = int(req.calculate_cost(is_opus=request.app.state.config.novelai.account_tier >= 3))
    except Exception as exc:
        return JSONResponse(status_code=400, content={"message": "Failed to calculate anlas cost"})

    return await _submit_zip_task(
        request=request,
        user=user,
        action=str(req.action),
        metadata=_generate_metadata(req),
        estimated_cost=estimated_cost,
        handler=lambda: request.app.state.upstream.generate_image_zip(req),
    )


@router.post("/ai/upscale")
async def upscale(
    req: Upscale,
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    return await _submit_zip_task(
        request=request,
        user=user,
        action="upscale",
        metadata={
            "model": None,
            "width": req.width,
            "height": req.height,
            "steps": None,
            "n_samples": 1,
        },
        estimated_cost=request.app.state.config.novelai.upscale_anlas_cost,
        handler=lambda: request.app.state.upstream.upscale_zip(req),
    )


@router.post("/ai/augment-image")
async def augment_image(
    req: AugmentImageInfer,
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    try:
        estimated_cost = int(req.calculate_cost(is_opus=request.app.state.config.novelai.account_tier >= 3))
    except Exception:
        return JSONResponse(status_code=400, content={"message": "Failed to calculate anlas cost"})

    return await _submit_zip_task(
        request=request,
        user=user,
        action=str(req.req_type.value),
        metadata={
            "model": "nai-diffusion-3",
            "width": req.width,
            "height": req.height,
            "steps": 28,
            "n_samples": 1,
        },
        estimated_cost=estimated_cost,
        handler=lambda: request.app.state.upstream.augment_image_zip(req),
    )


async def _submit_zip_task(
    *,
    request: Request,
    user: UserContext,
    action: str,
    metadata: dict[str, Any],
    estimated_cost: int,
    handler: Callable[[], Awaitable[bytes]],
):
    db: Database = request.app.state.db
    rate_limiter: RateLimiter = request.app.state.rate_limiter
    quota_manager: QuotaManager = request.app.state.quota_manager
    proxy_queue: ProxyQueue = request.app.state.proxy_queue
    request_id = uuid.uuid4().hex

    rate = rate_limiter.check(user.id)
    if not rate.allowed:
        _insert_usage_log(db, request_id, user.id, action, metadata, 0, "rejected", "rate_limited", rate.message)
        return JSONResponse(
            status_code=429,
            content={"message": rate.message, "retry_after": rate.retry_after},
            headers={"Retry-After": str(rate.retry_after)},
        )

    if estimated_cost < 0:
        _insert_usage_log(
            db,
            request_id,
            user.id,
            action,
            metadata,
            estimated_cost,
            "rejected",
            "unsupported_cost",
            "Request exceeds supported cost bounds",
        )
        return JSONResponse(status_code=400, content={"message": "Request exceeds supported cost bounds"})

    try:
        quota_manager.reserve(user.id, estimated_cost)
    except InsufficientQuota as exc:
        _insert_usage_log(
            db,
            request_id,
            user.id,
            action,
            metadata,
            estimated_cost,
            "rejected",
            "insufficient_anlas",
            str(exc),
        )
        return JSONResponse(
            status_code=402,
            content={"message": str(exc), "need": exc.need, "have": exc.have},
        )

    _insert_usage_log(db, request_id, user.id, action, metadata, estimated_cost, "queued", None, None)

    try:
        zip_payload = await proxy_queue.submit(
            request_id=request_id,
            user_id=user.id,
            tier=user.tier,
            estimated_cost=estimated_cost,
            handler=handler,
        )
    except QueueFull:
        quota_manager.release(user.id, estimated_cost)
        db.execute(
            """
            UPDATE usage_logs
            SET status = 'rejected',
                error_code = 'queue_full',
                error_message = 'Queue full, please retry later',
                completed_at = ?
            WHERE request_id = ?
            """,
            (utc_now_iso(), request_id),
        )
        return JSONResponse(status_code=503, content={"message": "Queue full, please retry later"})
    except APIError as exc:
        status_code = int(exc.code) if str(exc.code or "").isdigit() else 502
        return JSONResponse(
            status_code=status_code,
            content=exc.response if isinstance(exc.response, dict) else {"message": exc.message},
        )
    except Exception as exc:
        return JSONResponse(status_code=502, content={"message": str(exc)})

    return Response(
        content=zip_payload,
        status_code=201,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment;filename=image.zip"},
    )


@router.get("/ai/generate-image/suggest-tags")
async def suggest_tags(
    model: str,
    prompt: str,
    request: Request,
    lang: str = "en",
    user: UserContext = Depends(get_current_user),
):
    del user
    try:
        return await request.app.state.upstream.suggest_tags(model=model, prompt=prompt, lang=lang)
    except APIError as exc:
        status_code = int(exc.code) if str(exc.code or "").isdigit() else 502
        return JSONResponse(
            status_code=status_code,
            content=exc.response if isinstance(exc.response, dict) else {"message": exc.message},
        )


@router.get("/ai/generate-image/suggest_tags")
async def suggest_tags_alias(
    model: str,
    prompt: str,
    request: Request,
    lang: str = "en",
    user: UserContext = Depends(get_current_user),
):
    return await suggest_tags(model=model, prompt=prompt, request=request, lang=lang, user=user)


@router.get("/user/subscription")
async def subscription(
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    quota = request.app.state.quota_manager.get_snapshot(user.id)
    return {
        "tier": 3 if user.tier == "vip" else 1,
        "active": True,
        "expiresAt": 0,
        "perks": {
            "maxPriorityActions": 0,
            "startPriority": 0,
            "moduleTrainingSteps": 0,
            "unlimitedMaxPriority": False,
            "voiceGeneration": False,
            "imageGeneration": True,
            "unlimitedImageGeneration": quota.available > 0,
            "unlimitedImageGenerationLimits": [],
            "contextTokens": 0,
        },
        "paymentProcessorData": None,
        "trainingStepsLeft": {
            "fixedTrainingStepsLeft": quota.available,
            "purchasedTrainingSteps": 0,
        },
        "accountType": 0,
        "proxyQuota": {
            "total": quota.total,
            "used": quota.used,
            "reserved": quota.reserved,
            "available": quota.available,
        },
    }


def _insert_usage_log(
    db: Database,
    request_id: str,
    user_id: int,
    action: str,
    metadata: dict[str, Any],
    estimated_cost: int,
    status: str,
    error_code: str | None,
    error_message: str | None,
) -> None:
    db.execute(
        """
        INSERT INTO usage_logs (
            request_id, user_id, action, model, width, height, steps, n_samples,
            estimated_anlas_cost, status, error_code, error_message, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            user_id,
            action,
            metadata.get("model"),
            _optional_int(metadata.get("width")),
            _optional_int(metadata.get("height")),
            _optional_int(metadata.get("steps")),
            _optional_int(metadata.get("n_samples")),
            int(estimated_cost),
            status,
            error_code,
            error_message,
            utc_now_iso(),
        ),
    )


def _generate_metadata(req: GenerateImageInfer) -> dict[str, Any]:
    params = req.parameters
    return {
        "model": str(req.model),
        "width": params.width,
        "height": params.height,
        "steps": params.steps,
        "n_samples": params.n_samples,
    }


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
