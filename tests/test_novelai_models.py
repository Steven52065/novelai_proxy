from __future__ import annotations

import base64
import io

import pytest
from PIL import Image
from pydantic import ValidationError

from app.novelai_models import (
    AugmentImageRequest,
    Moods,
    ReqType,
    UpscaleRequest,
)


def _png_base64(width: int = 3, height: int = 2) -> str:
    output = io.BytesIO()
    Image.new("RGB", (width, height), color=(255, 0, 0)).save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def test_upscale_detects_dimensions_and_preserves_payload_shape():
    request = UpscaleRequest(image=_png_base64(), scale=2, ignored="value")

    assert request.width == 3
    assert request.height == 2
    assert request.model_dump(mode="json", exclude_none=True) == {
        "image": request.image,
        "width": 3,
        "height": 2,
        "scale": 2.0,
    }


def test_upscale_encodes_bytes_and_allows_explicit_dimensions_for_opaque_base64():
    encoded = UpscaleRequest(image=b"img", width=64, height=32)
    opaque = UpscaleRequest(image="aW1n", width=64, height=32)

    assert encoded.image == "aW1n"
    assert opaque.width == 64
    assert opaque.height == 32


def test_upscale_rejects_data_url_and_missing_detectable_dimensions():
    with pytest.raises(ValidationError):
        UpscaleRequest(image="data:image/png;base64,aW1n", width=64, height=64)
    with pytest.raises(ValidationError):
        UpscaleRequest(image="aW1n")


def test_augment_serialization_matches_sdk_request_fields():
    request = AugmentImageRequest(
        req_type="sketch",
        width=64,
        height=32,
        image=b"img",
        defry=0,
        ignored="value",
    )

    assert request.req_type is ReqType.SKETCH
    assert request.model_dump(mode="json", exclude_none=True) == {
        "req_type": "sketch",
        "width": 64,
        "height": 32,
        "image": "aW1n",
        "defry": 0,
    }


@pytest.mark.parametrize("image", ["data:image/png;base64,aW1n", "+vv-invalid"])
def test_augment_rejects_invalid_image_prefixes(image: str):
    with pytest.raises(ValidationError):
        AugmentImageRequest(req_type="sketch", width=64, height=64, image=image)


def test_augment_emotion_prompt_requires_known_mood_prefix():
    request = AugmentImageRequest(
        req_type="emotion",
        width=64,
        height=64,
        image="aW1n",
        prompt="happy;;smile",
    )
    assert request.req_type is ReqType.EMOTION

    with pytest.raises(ValidationError):
        AugmentImageRequest(
            req_type="emotion",
            width=64,
            height=64,
            image="aW1n",
            prompt="smile",
        )


def test_director_tool_enums_match_removed_sdk_contract():
    assert [member.value for member in ReqType] == [
        "bg-removal",
        "colorize",
        "lineart",
        "sketch",
        "emotion",
        "declutter",
        "declutter-keep-bubbles",
    ]
    assert len(Moods) == 24
    assert Moods.Saf.value == "sad"
