from __future__ import annotations

import math
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
from ..logging_utils import dump_model_payload, json_dumps, logger
from ..queue_manager import ProxyQueue, QueueFull
from ..quota_manager import InsufficientQuota, QuotaManager
from ..rate_limiter import RateLimiter


router = APIRouter()

IMAGE_ANLAS_PER_PRECISE_REFERENCE = 5
IMAGE_ANLAS_PER_VIBE_ENCODING = 2
FREE_VIBE_REFERENCES_PER_GENERATION = 4
IMAGE_ANLAS_PER_EXTRA_VIBE_REFERENCE = 2


@router.get("/health")
async def health(request: Request):
    return {
        "status": "ok",
        "queue_size": request.app.state.proxy_queue.qsize(),
    }


@router.post("/ai/generate-image")
async def generate_image(
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    payload = await request.json()
    normalized_payload = _normalize_generate_image_payload(payload)
    try:
        req = GenerateImageInfer.model_validate(normalized_payload)
    except Exception as exc:
        logger.error("generate-image payload validation failed after normalization errors=%s", str(exc))
        return JSONResponse(status_code=400, content={"message": "Invalid request", "details": str(exc)})

    request_payload = _merge_generate_payload(dump_model_payload(req), normalized_payload)
    _apply_image_format_policy(request_payload, request.app.state.config.image_format)
    try:
        estimated_cost = _calculate_generate_cost(
            req,
            request_payload,
            is_opus=request.app.state.config.novelai.account_tier >= 3,
        )
    except Exception:
        return JSONResponse(status_code=400, content={"message": "Failed to calculate anlas cost"})

    return await _submit_zip_task(
        request=request,
        user=user,
        action=str(req.action),
        metadata=_generate_metadata(req),
        request_payload=request_payload,
        estimated_cost=estimated_cost,
        handler=lambda: request.app.state.upstream.generate_image_payload_zip(request_payload),
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
        request_payload=dump_model_payload(req),
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
        request_payload=dump_model_payload(req),
        estimated_cost=estimated_cost,
        handler=lambda: request.app.state.upstream.augment_image_zip(req),
    )


@router.post("/ai/encode-vibe")
async def encode_vibe(
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"message": "Invalid request"})

    return await _submit_binary_task(
        request=request,
        user=user,
        action="encode-vibe",
        metadata={
            "model": payload.get("model"),
            "width": None,
            "height": None,
            "steps": None,
            "n_samples": 1,
        },
        request_payload=payload,
        estimated_cost=IMAGE_ANLAS_PER_VIBE_ENCODING,
        handler=lambda: request.app.state.upstream.encode_vibe_binary(payload),
        media_type="application/binary",
        process_zip_response=False,
    )


async def _submit_zip_task(
    *,
    request: Request,
    user: UserContext,
    action: str,
    metadata: dict[str, Any],
    request_payload: dict[str, Any],
    estimated_cost: int,
    handler: Callable[[], Awaitable[bytes]],
):
    return await _submit_binary_task(
        request=request,
        user=user,
        action=action,
        metadata=metadata,
        request_payload=request_payload,
        estimated_cost=estimated_cost,
        handler=handler,
        media_type="application/zip",
        response_headers={"Content-Disposition": "attachment;filename=image.zip"},
        process_zip_response=True,
    )


async def _submit_binary_task(
    *,
    request: Request,
    user: UserContext,
    action: str,
    metadata: dict[str, Any],
    request_payload: dict[str, Any],
    estimated_cost: int,
    handler: Callable[[], Awaitable[bytes]],
    media_type: str,
    response_headers: dict[str, str] | None = None,
    process_zip_response: bool = True,
):
    db: Database = request.app.state.db
    rate_limiter: RateLimiter = request.app.state.rate_limiter
    quota_manager: QuotaManager = request.app.state.quota_manager
    proxy_queue: ProxyQueue = request.app.state.proxy_queue
    request_id = uuid.uuid4().hex
    logger.debug(
        "proxy request received request_id=%s user_id=%s action=%s payload=%s",
        request_id,
        user.id,
        action,
        json_dumps(request_payload),
    )

    rate = rate_limiter.check(user.id)
    if not rate.allowed:
        _insert_usage_log(
            db,
            request_id,
            user.id,
            action,
            metadata,
            request_payload,
            0,
            "rejected",
            "rate_limited",
            rate.message,
            "INFO",
        )
        logger.info("proxy request rate limited request_id=%s user_id=%s", request_id, user.id)
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
            request_payload,
            estimated_cost,
            "rejected",
            "unsupported_cost",
            "Request exceeds supported cost bounds",
            "ERROR",
        )
        logger.error("proxy request has unsupported cost request_id=%s estimated_cost=%s", request_id, estimated_cost)
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
            request_payload,
            estimated_cost,
            "rejected",
            "insufficient_anlas",
            str(exc),
            "INFO",
        )
        logger.info("proxy request rejected for quota request_id=%s user_id=%s need=%s have=%s", request_id, user.id, exc.need, exc.have)
        return JSONResponse(
            status_code=402,
            content={"message": str(exc), "need": exc.need, "have": exc.have},
        )

    _insert_usage_log(
        db,
        request_id,
        user.id,
        action,
        metadata,
        request_payload,
        estimated_cost,
        "queued",
        None,
        None,
        "INFO",
    )
    logger.info("proxy request queued request_id=%s user_id=%s action=%s estimated_cost=%s", request_id, user.id, action, estimated_cost)

    try:
        binary_payload = await proxy_queue.submit(
            request_id=request_id,
            user_id=user.id,
            tier=user.tier,
            action=action,
            logging_config=request.app.state.config.logging,
            estimated_cost=estimated_cost,
            handler=handler,
            process_zip_response=process_zip_response,
        )
    except QueueFull:
        quota_manager.release(user.id, estimated_cost)
        db.execute(
            """
            UPDATE usage_logs
            SET status = 'rejected',
                error_code = 'queue_full',
                error_message = 'Queue full, please retry later',
                log_level = 'ERROR',
                completed_at = ?
            WHERE request_id = ?
            """,
            (utc_now_iso(), request_id),
        )
        logger.error("proxy queue full request_id=%s user_id=%s", request_id, user.id)
        return JSONResponse(status_code=503, content={"message": "Queue full, please retry later"})
    except APIError as exc:
        status_code = int(exc.code) if str(exc.code or "").isdigit() else 502
        logger.error("upstream API error request_id=%s code=%s message=%s", request_id, exc.code, exc.message)
        return JSONResponse(
            status_code=status_code,
            content=exc.response if isinstance(exc.response, dict) else {"message": exc.message},
        )
    except Exception as exc:
        logger.exception("proxy request failed request_id=%s", request_id)
        return JSONResponse(status_code=502, content={"message": str(exc)})

    return Response(
        content=binary_payload,
        status_code=201,
        media_type=media_type,
        headers=response_headers,
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
    request_payload: dict[str, Any],
    estimated_cost: int,
    status: str,
    error_code: str | None,
    error_message: str | None,
    log_level: str,
) -> None:
    db.execute(
        """
        INSERT INTO usage_logs (
            request_id, user_id, action, model, width, height, steps, n_samples,
            estimated_anlas_cost, status, error_code, error_message, log_level, request_payload, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            log_level,
            json_dumps(request_payload),
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


def _calculate_generate_cost(req: GenerateImageInfer, payload: dict[str, Any], *, is_opus: bool) -> int:
    base_cost = int(req.calculate_cost(is_opus=is_opus))
    params = _payload_parameters(payload)
    return base_cost + _reference_anlas_cost(params)


def _reference_anlas_cost(parameters: dict[str, Any]) -> int:
    precise_references = _reference_count(parameters, "director_reference_images")

    vibe_references = _reference_count(parameters, "reference_image")
    vibe_references += _reference_count(parameters, "reference_image_multiple")
    extra_vibes = max(vibe_references - FREE_VIBE_REFERENCES_PER_GENERATION, 0)
    return (
        precise_references * IMAGE_ANLAS_PER_PRECISE_REFERENCE
        + extra_vibes * IMAGE_ANLAS_PER_EXTRA_VIBE_REFERENCE
    )


def _reference_count(parameters: dict[str, Any], key: str) -> int:
    value = parameters.get(key)
    if isinstance(value, list):
        return sum(1 for item in value if item)
    if value:
        return 1
    return 0


def _merge_generate_payload(validated_payload: dict[str, Any], original_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(validated_payload)
    for key, value in original_payload.items():
        if key == "parameters":
            continue
        if key not in payload:
            payload[key] = value

    merged_parameters = dict(validated_payload.get("parameters") or {})
    original_parameters = _payload_parameters(original_payload)
    for key, value in original_parameters.items():
        if key not in merged_parameters:
            merged_parameters[key] = value
    payload["parameters"] = merged_parameters
    return payload


def _payload_parameters(payload: dict[str, Any]) -> dict[str, Any]:
    parameters = payload.get("parameters")
    return parameters if isinstance(parameters, dict) else {}


def _apply_image_format_policy(payload: dict[str, Any], config) -> None:
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        return
    if config.mode == "force":
        parameters["image_format"] = config.format


def _normalize_generate_image_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"message": "Invalid request"})

    normalized = dict(payload)
    parameters = normalized.get("parameters")
    if not isinstance(parameters, dict):
        return normalized

    normalized_parameters = dict(parameters)
    _normalize_img2img_parameters(normalized_parameters)
    _normalize_int_ceiling(normalized_parameters, "skip_cfg_above_sigma")
    _normalize_seed(normalized_parameters, "seed")
    _normalize_seed(normalized_parameters, "extra_noise_seed")
    normalized["parameters"] = normalized_parameters
    return normalized


def _normalize_img2img_parameters(parameters: dict[str, Any]) -> None:
    img2img = parameters.get("img2img")
    if not isinstance(img2img, dict):
        return
    for key in ("strength", "noise", "extra_noise_seed", "color_correct"):
        if key not in parameters and key in img2img:
            parameters[key] = img2img[key]


def _normalize_int_ceiling(parameters: dict[str, Any], key: str) -> None:
    value = parameters.get(key)
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, float):
        parameters[key] = math.ceil(value)


def _normalize_seed(parameters: dict[str, Any], key: str) -> None:
    value = parameters.get(key)
    if value is None or isinstance(value, bool):
        return
    try:
        seed = int(value)
    except (TypeError, ValueError):
        return
    max_seed = 4294967295 - 7
    if seed <= 0 or seed > max_seed:
        seed = seed % max_seed
        if seed == 0:
            seed = max_seed
    parameters[key] = seed


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
