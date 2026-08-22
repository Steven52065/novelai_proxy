from __future__ import annotations

import base64
from enum import Enum
from io import BytesIO

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReqType(Enum):
    """
    Director-tool operation types supported by NovelAI.
    """

    BG_REMOVAL = "bg-removal"
    COLORIZE = "colorize"
    LINEART = "lineart"
    SKETCH = "sketch"
    EMOTION = "emotion"
    DECLUTTER = "declutter"
    DECLUTTER_KEEP_BUBBLES = "declutter-keep-bubbles"


class Moods(Enum):
    """Mood prefixes accepted by the emotion director tool."""

    Neutral = "neutral"
    Happy = "happy"
    Saf = "sad"
    Angry = "angry"
    Scared = "scared"
    Surprised = "surprised"
    Tired = "tired"
    Excited = "excited"
    Nervous = "nervous"
    Thinking = "thinking"
    Confused = "confused"
    Shy = "shy"
    Disgusted = "disgusted"
    Smug = "smug"
    Bored = "bored"
    Laughing = "laughing"
    Irritated = "irritated"
    Aroused = "aroused"
    Embarrassed = "embarrassed"
    Worried = "worried"
    Love = "love"
    Determined = "determined"
    Hurt = "hurt"
    Playful = "playful"


class UpscaleRequest(BaseModel):
    """Validated request body for ``/ai/upscale``."""

    image: str | bytes
    width: int | None = None
    height: int | None = None
    scale: float = 4

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def validate_image(self) -> "UpscaleRequest":
        if isinstance(self.image, str) and self.image.startswith("data:image/"):
            raise ValueError("Invalid image format, must be base64 encoded.")
        if isinstance(self.image, bytes):
            self.image = base64.b64encode(self.image).decode("utf-8")

        detected = _detect_image_size(self.image)
        if detected is None:
            # Test clients and archived payloads may contain opaque base64
            # placeholders.  Explicit dimensions are sufficient in that case.
            if self.width is None or self.height is None:
                raise ValueError(
                    "Invalid image size and cant auto detect, must be set width and height."
                )
        else:
            # 探测成功时以真实尺寸为准：/ai/upscale 的计费直接取这两个字段，
            # 若信任客户端声明，用户可谎报小尺寸以最低价放大大图。
            self.width, self.height = detected
        return self


class AugmentImageRequest(BaseModel):
    """Validated request body for ``/ai/augment-image``."""

    req_type: ReqType = Field(..., description="Type of augmentation")
    width: int = Field(..., description="Width of the image")
    height: int = Field(..., description="Height of the image")
    image: str | bytes = Field(..., description="Base64 encoded image")
    prompt: str | None = None
    defry: int | None = Field(0, ge=0, le=5, multiple_of=1)

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def validate_image_and_prompt(self) -> "AugmentImageRequest":
        if isinstance(self.image, bytes):
            self.image = base64.b64encode(self.image).decode("utf-8")
        if isinstance(self.image, str) and self.image.startswith("data:"):
            raise ValueError("Invalid `image` format, must be base64 encoded directly.")
        if isinstance(self.image, str) and self.image.startswith("+vv"):
            raise ValueError("Invalid `image` format, must be encoded correctly.")
        if self.prompt and self.req_type == ReqType.EMOTION:
            valid_starts = [mood.value for mood in Moods]
            if not any(self.prompt.startswith(f"{start};;") for start in valid_starts):
                raise ValueError(
                    f"Invalid `prompt` format, must start with one of {valid_starts}."
                )
        # 与 UpscaleRequest 同理：augment 的计费也直接取 width/height，
        # 能从图片探测出真实尺寸时就不采信客户端声明。
        detected = _detect_image_size(self.image)
        if detected is not None:
            self.width, self.height = detected
        return self


def _detect_image_size(image: str | bytes) -> tuple[int, int] | None:
    """Return the real pixel size of a base64 image, or ``None`` if undetectable."""
    try:
        decoded = base64.b64decode(image)
        with Image.open(BytesIO(decoded)) as opened:
            return opened.size
    except Exception:
        return None
