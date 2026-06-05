from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from json import JSONDecodeError
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from novelai_python._exceptions import APIError
from novelai_python.sdk.ai.augment_image import AugmentImageInfer
from novelai_python.sdk.ai.upscale import Upscale

from ..auth import UserContext, get_current_user
from ..costing import GenerateCostEstimator, GenerateCostInputs, IMAGE_ANLAS_PER_VIBE_ENCODING
from ..logging_utils import dump_model_payload, logger
from .service import MESSAGE_UPSTREAM_REQUEST_FAILED, ProxyRequestService, ProxyTaskRequest, ProxyTaskResult


router = APIRouter()
_generate_cost_estimator = GenerateCostEstimator()
ENDPOINT_GENERATE_IMAGE = "generate-image"
ENDPOINT_UPSCALE = "upscale"
ENDPOINT_AUGMENT_IMAGE = "augment-image"
ENDPOINT_ENCODE_VIBE = "encode-vibe"
ENDPOINT_SUGGEST_TAGS = "suggest-tags"


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
    endpoint_denied = _reject_disallowed_endpoint(user, ENDPOINT_GENERATE_IMAGE)
    if endpoint_denied is not None:
        return endpoint_denied

    payload, parse_error_response = await _read_json_payload(request, ENDPOINT_GENERATE_IMAGE)
    if parse_error_response is not None:
        return parse_error_response

    try:
        request_payload = _normalize_generate_image_payload(payload)
        cost_inputs = _generate_cost_estimator.extract_inputs(request_payload)
    except Exception as exc:
        logger.error("generate-image payload validation failed errors=%s", str(exc))
        return JSONResponse(status_code=400, content={"message": "Invalid request"})

    _apply_image_format_policy(request_payload, request.app.state.config.image_format)
    try:
        estimated_cost, cost_is_certainly_free = _generate_cost_estimator.calculate(
            cost_inputs,
            is_opus=request.app.state.config.novelai.account_tier >= 3,
        )
    except Exception:
        return JSONResponse(status_code=400, content={"message": "Failed to calculate anlas cost"})

    return await _submit_zip_task(
        request=request,
        user=user,
        action=cost_inputs.action,
        metadata=_generate_metadata(cost_inputs),
        request_payload=request_payload,
        estimated_cost=estimated_cost,
        free_small_only_allowed=cost_is_certainly_free,
        handler=lambda upstream: upstream.generate_image_payload_zip(request_payload),
    )


@router.post("/ai/upscale")
async def upscale(
    req: Upscale,
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    endpoint_denied = _reject_disallowed_endpoint(user, ENDPOINT_UPSCALE)
    if endpoint_denied is not None:
        return endpoint_denied

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
        handler=lambda upstream: upstream.upscale_zip(req),
    )


@router.post("/ai/augment-image")
async def augment_image(
    req: AugmentImageInfer,
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    endpoint_denied = _reject_disallowed_endpoint(user, ENDPOINT_AUGMENT_IMAGE)
    if endpoint_denied is not None:
        return endpoint_denied

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
        handler=lambda upstream: upstream.augment_image_zip(req),
    )


@router.post("/ai/encode-vibe")
async def encode_vibe(
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    endpoint_denied = _reject_disallowed_endpoint(user, ENDPOINT_ENCODE_VIBE)
    if endpoint_denied is not None:
        return endpoint_denied

    payload, parse_error_response = await _read_json_payload(request, ENDPOINT_ENCODE_VIBE)
    if parse_error_response is not None:
        return parse_error_response

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
        handler=lambda upstream: upstream.encode_vibe_binary(payload),
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
    handler: Callable[[Any], Awaitable[bytes]],
    free_small_only_allowed: bool = False,
):
    result = await _proxy_service(request).submit_zip(
        ProxyTaskRequest(
            user=user,
            action=action,
            metadata=metadata,
            request_payload=request_payload,
            estimated_cost=estimated_cost,
            handler=handler,
            free_small_only_allowed=free_small_only_allowed,
            process_zip_response=True,
        )
    )
    return _task_result_to_response(request, result)


async def _submit_binary_task(
    *,
    request: Request,
    user: UserContext,
    action: str,
    metadata: dict[str, Any],
    request_payload: dict[str, Any],
    estimated_cost: int,
    handler: Callable[[Any], Awaitable[bytes]],
    media_type: str,
    free_small_only_allowed: bool = False,
    response_headers: dict[str, str] | None = None,
    process_zip_response: bool = True,
):
    result = await _proxy_service(request).submit_binary(
        ProxyTaskRequest(
            user=user,
            action=action,
            metadata=metadata,
            request_payload=request_payload,
            estimated_cost=estimated_cost,
            handler=handler,
            free_small_only_allowed=free_small_only_allowed,
            process_zip_response=process_zip_response,
        ),
        media_type=media_type,
        response_headers=response_headers,
    )
    return _task_result_to_response(request, result)


@router.get("/ai/generate-image/suggest-tags")
async def suggest_tags(
    model: str,
    prompt: str,
    request: Request,
    lang: str = "en",
    user: UserContext = Depends(get_current_user),
):
    endpoint_denied = _reject_disallowed_endpoint(user, ENDPOINT_SUGGEST_TAGS)
    if endpoint_denied is not None:
        return endpoint_denied

    try:
        # suggest-tags is kept as a lightweight compatibility endpoint for now:
        # it only selects a user-allowed upstream client and does not enter the
        # generation queue, so it is not protected by per-upstream queue delays.
        upstream = request.app.state.proxy_queue.select_client(user.allowed_upstreams)
        return await upstream.suggest_tags(model=model, prompt=prompt, lang=lang)
    except APIError as exc:
        status_code = int(exc.code) if str(exc.code or "").isdigit() else 502
        logger.error("suggest-tags upstream API error code=%s message=%s", exc.code, exc.message)
        return JSONResponse(
            status_code=status_code,
            content={"message": MESSAGE_UPSTREAM_REQUEST_FAILED},
        )
    except Exception as exc:
        logger.exception("suggest-tags request failed")
        return JSONResponse(status_code=503, content={"message": "Suggest tags request failed"})


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


def _proxy_service(request: Request) -> ProxyRequestService:
    return request.app.state.proxy_service


async def _read_json_payload(request: Request, endpoint: str) -> tuple[Any, JSONResponse | None]:
    try:
        return await request.json(), None
    except (JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("%s JSON parsing failed errors=%s", endpoint, str(exc))
        return None, JSONResponse(status_code=400, content={"message": "Invalid request"})


def _task_result_to_response(request: Request, result: ProxyTaskResult) -> Response | JSONResponse:
    _mark_total_duration(request, result.request_id)
    if result.is_binary:
        return Response(
            content=result.content,
            status_code=result.status_code,
            media_type=result.media_type,
            headers=result.headers,
        )
    return JSONResponse(
        status_code=result.status_code,
        content=result.content,
        headers=result.headers,
    )


def _mark_total_duration(request: Request, request_id: str | None) -> None:
    if not request_id:
        return
    started_at = getattr(request.state, "request_started_at", None)
    try:
        total_ms = int((time.perf_counter() - float(started_at)) * 1000)
    except (TypeError, ValueError):
        return
    request.app.state.usage_logs.mark_total_duration(request_id, max(0, total_ms))


def _generate_metadata(params: GenerateCostInputs) -> dict[str, Any]:
    return {
        "model": params.model,
        "width": params.width,
        "height": params.height,
        "steps": params.steps,
        "n_samples": params.n_samples,
    }


def _reject_disallowed_endpoint(user: UserContext, endpoint: str) -> JSONResponse | None:
    if endpoint in user.allowed_endpoints:
        return None
    return JSONResponse(
        status_code=403,
        content={"message": f"User is not allowed to access endpoint: {endpoint}"},
    )


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
    normalized["parameters"] = dict(parameters)
    return normalized
