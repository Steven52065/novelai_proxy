from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from novelai_python._exceptions import APIError
from novelai_python.sdk.ai._cost import CostCalculator
from novelai_python.sdk.ai._enum import Action, Model, Sampler
from novelai_python.sdk.ai.augment_image import AugmentImageInfer
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
ENDPOINT_GENERATE_IMAGE = "generate-image"
ENDPOINT_UPSCALE = "upscale"
ENDPOINT_AUGMENT_IMAGE = "augment-image"
ENDPOINT_ENCODE_VIBE = "encode-vibe"
ENDPOINT_SUGGEST_TAGS = "suggest-tags"
FREE_SMALL_ONLY_ALLOWED_PARAMETERS = {
    "width",
    "height",
    "scale",
    "sampler",
    "steps",
    "n_samples",
    "ucPreset",
    "qualityToggle",
    "sm",
    "sm_dyn",
    "seed",
    "negative_prompt",
    "noise_schedule",
    "cfg_rescale",
    "dynamic_thresholding",
    "controlnet_strength",
    "legacy",
    "legacy_v3_extend",
    "uncond_scale",
    "deliberate_euler_ancestral_bug",
    "prefer_brownian",
    "image_format",
    "skip_cfg_above_sigma",
    "characterPrompts",
    "v4_prompt",
    "v4_negative_prompt",
    "use_coords",
    "legacy_uc",
    "add_original_image",
    "autoSmea",
    "params_version",
}
FREE_SMALL_ONLY_FORBIDDEN_PARAMETERS = {
    "image",
    "mask",
    "strength",
    "noise",
    "extra_noise_seed",
    "reference_image",
    "reference_image_multiple",
    "reference_strength_multiple",
    "reference_information_extracted_multiple",
    "director_reference_images",
    "director_reference_descriptions",
    "director_reference_strength_values",
    "director_reference_secondary_strength_values",
    "director_reference_information_extracted",
    "controlnet_condition",
    "controlnet_model",
}


@dataclass(frozen=True)
class GenerateCostInputs:
    model: str
    action: str
    width: int
    height: int
    steps: int
    n_samples: int
    sampler: Sampler | None
    sampler_was_known: bool
    sm: bool
    sm_dyn: bool
    image: bool
    strength: float | None
    reference_cost: int
    free_small_only_parameters_safe: bool


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

    payload = await request.json()
    try:
        request_payload = _normalize_generate_image_payload(payload)
        cost_inputs = _extract_generate_cost_inputs(request_payload)
    except Exception as exc:
        logger.error("generate-image payload validation failed errors=%s", str(exc))
        return JSONResponse(status_code=400, content={"message": "Invalid request", "details": str(exc)})

    _apply_image_format_policy(request_payload, request.app.state.config.image_format)
    try:
        estimated_cost, cost_is_certainly_free = _calculate_generate_cost(
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
        handler=lambda: request.app.state.upstream.generate_image_payload_zip(request_payload),
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
        handler=lambda: request.app.state.upstream.upscale_zip(req),
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
        handler=lambda: request.app.state.upstream.augment_image_zip(req),
    )


@router.post("/ai/encode-vibe")
async def encode_vibe(
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    endpoint_denied = _reject_disallowed_endpoint(user, ENDPOINT_ENCODE_VIBE)
    if endpoint_denied is not None:
        return endpoint_denied

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
    free_small_only_allowed: bool = False,
):
    return await _submit_binary_task(
        request=request,
        user=user,
        action=action,
        metadata=metadata,
        request_payload=request_payload,
        estimated_cost=estimated_cost,
        handler=handler,
        free_small_only_allowed=free_small_only_allowed,
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
    free_small_only_allowed: bool = False,
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

    if user.free_small_only and not free_small_only_allowed:
        _insert_usage_log(
            db,
            request_id,
            user.id,
            action,
            metadata,
            request_payload,
            estimated_cost,
            "rejected",
            "free_small_only_blocked",
            "User is limited to definitely free small image generations",
            "INFO",
        )
        logger.info(
            "proxy request rejected by free small only request_id=%s user_id=%s action=%s estimated_cost=%s",
            request_id,
            user.id,
            action,
            estimated_cost,
        )
        return JSONResponse(
            status_code=403,
            content={"message": "User is limited to definitely free small image generations"},
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
    endpoint_denied = _reject_disallowed_endpoint(user, ENDPOINT_SUGGEST_TAGS)
    if endpoint_denied is not None:
        return endpoint_denied

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


def _generate_metadata(params: GenerateCostInputs) -> dict[str, Any]:
    return {
        "model": params.model,
        "width": params.width,
        "height": params.height,
        "steps": params.steps,
        "n_samples": params.n_samples,
    }


def _calculate_generate_cost(params: GenerateCostInputs, *, is_opus: bool) -> tuple[int, bool]:
    base_cost = int(
        CostCalculator.calculate(
            width=params.width,
            height=params.height,
            steps=params.steps,
            model=params.model,
            image=params.image,
            n_samples=params.n_samples,
            account_tier=3 if is_opus else 1,
            strength=params.strength,
            sampler=params.sampler,
            is_sm_enabled=params.sm,
            is_sm_dynamic=params.sm_dyn,
            is_account_active=True,
        )
    )
    total_cost = base_cost + params.reference_cost
    is_certainly_free = (
        is_opus
        and total_cost == 0
        and base_cost == 0
        and params.reference_cost == 0
        and _is_known_text_to_image_model(params.model)
        and params.action == Action.GENERATE.value
        and params.free_small_only_parameters_safe
        and params.n_samples == 1
        and params.steps <= 28
        and params.width * params.height <= 1048576
        and params.sampler_was_known
        and not params.image
    )
    return total_cost, is_certainly_free


def _extract_generate_cost_inputs(payload: dict[str, Any]) -> GenerateCostInputs:
    model = payload.get("model")
    action = payload.get("action", "generate")
    parameters = payload.get("parameters")
    if not isinstance(model, str) or not model:
        raise ValueError("model is required")
    if not isinstance(action, str) or not action:
        raise ValueError("action is required")
    if not isinstance(parameters, dict):
        raise ValueError("parameters is required")

    sampler_value = parameters.get("sampler")
    sampler, sampler_was_known = _optional_sampler(sampler_value)
    return GenerateCostInputs(
        model=model,
        action=action,
        width=_required_int(parameters, "width", minimum=64),
        height=_required_int(parameters, "height", minimum=64),
        steps=_required_int(parameters, "steps", minimum=1),
        n_samples=_required_int(parameters, "n_samples", minimum=1),
        sampler=sampler,
        sampler_was_known=sampler_was_known,
        sm=_optional_bool(parameters.get("sm")),
        sm_dyn=_optional_bool(parameters.get("sm_dyn")),
        image=bool(parameters.get("image")),
        strength=_optional_float(parameters.get("strength")),
        reference_cost=_reference_anlas_cost(parameters),
        free_small_only_parameters_safe=_free_small_only_parameters_are_safe(parameters),
    )


def _reject_disallowed_endpoint(user: UserContext, endpoint: str) -> JSONResponse | None:
    if endpoint in user.allowed_endpoints:
        return None
    return JSONResponse(
        status_code=403,
        content={"message": f"User is not allowed to access endpoint: {endpoint}"},
    )


def _is_known_text_to_image_model(model: str) -> bool:
    try:
        parsed = Model(model)
    except ValueError:
        return False
    return parsed not in {
        Model.NAI_DIFFUSION_4_5_FULL_INPAINTING,
        Model.NAI_DIFFUSION_4_5_CURATED_INPAINTING,
        Model.NAI_DIFFUSION_4_FULL_INPAINTING,
        Model.NAI_DIFFUSION_4_CURATED_INPAINTING,
        Model.NAI_DIFFUSION_3_INPAINTING,
        Model.NAI_DIFFUSION_FURRY_3_INPAINTING,
        Model.NAI_DIFFUSION_INPAINTING,
        Model.SAFE_DIFFUSION_INPAINTING,
        Model.FURRY_DIFFUSION_INPAINTING,
    }


def _free_small_only_parameters_are_safe(parameters: dict[str, Any]) -> bool:
    unknown_keys = set(parameters) - FREE_SMALL_ONLY_ALLOWED_PARAMETERS - FREE_SMALL_ONLY_FORBIDDEN_PARAMETERS
    if unknown_keys:
        return False
    return not any(_parameter_has_value(parameters.get(key)) for key in FREE_SMALL_ONLY_FORBIDDEN_PARAMETERS)


def _parameter_has_value(value: Any) -> bool:
    if value is None:
        return False
    if value is False:
        return False
    if isinstance(value, (str, bytes, list, dict, tuple, set)):
        return len(value) > 0
    return True


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


def _required_int(parameters: dict[str, Any], key: str, *, minimum: int) -> int:
    value = parameters.get(key)
    if value is None or isinstance(value, bool):
        raise ValueError(f"parameters.{key} is required")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"parameters.{key} must be an integer") from exc
    if number < minimum:
        raise ValueError(f"parameters.{key} must be >= {minimum}")
    return number


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _optional_sampler(value: Any) -> tuple[Sampler | None, bool]:
    if value is None:
        return None, False
    try:
        return Sampler(value), True
    except ValueError:
        return None, False


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
    normalized["parameters"] = dict(parameters)
    return normalized


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
