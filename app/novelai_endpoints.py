from __future__ import annotations

from collections.abc import Mapping


NOVELAI_IMAGE_API_BASE_URL = "https://image.novelai.net/ai"
NOVELAI_API_BASE_URL = "https://api.novelai.net/ai"

GENERATE_IMAGE_ENDPOINT = f"{NOVELAI_IMAGE_API_BASE_URL}/generate-image"
AUGMENT_IMAGE_ENDPOINT = f"{NOVELAI_IMAGE_API_BASE_URL}/augment-image"
ENCODE_VIBE_ENDPOINT = f"{NOVELAI_IMAGE_API_BASE_URL}/encode-vibe"
UPSCALE_ENDPOINT = f"{NOVELAI_API_BASE_URL}/upscale"


def replay_endpoint_for(action: str, payload: Mapping[str, object]) -> str | None:
    if action == "upscale":
        return UPSCALE_ENDPOINT
    if action == "encode-vibe":
        return ENCODE_VIBE_ENDPOINT
    if isinstance(payload.get("parameters"), dict) and isinstance(payload.get("model"), str):
        return GENERATE_IMAGE_ENDPOINT
    if isinstance(payload.get("req_type"), str):
        return AUGMENT_IMAGE_ENDPOINT
    return None
