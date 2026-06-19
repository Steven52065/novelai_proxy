from __future__ import annotations

from typing import Literal

from .config import ImageFormatConfig


IMAGE_FORMAT_POLICY_FOLLOW_GLOBAL = "follow_global"
IMAGE_FORMAT_POLICY_RESPECT_REQUEST = "respect_request"
IMAGE_FORMAT_POLICY_FORCE_PNG = "force_png"
IMAGE_FORMAT_POLICY_FORCE_WEBP = "force_webp"

ImageFormatPolicy = Literal["follow_global", "respect_request", "force_png", "force_webp"]

DEFAULT_IMAGE_FORMAT_POLICY: ImageFormatPolicy = IMAGE_FORMAT_POLICY_FOLLOW_GLOBAL
IMAGE_FORMAT_POLICY_VALUES: tuple[ImageFormatPolicy, ...] = (
    IMAGE_FORMAT_POLICY_FOLLOW_GLOBAL,
    IMAGE_FORMAT_POLICY_RESPECT_REQUEST,
    IMAGE_FORMAT_POLICY_FORCE_PNG,
    IMAGE_FORMAT_POLICY_FORCE_WEBP,
)

IMAGE_FORMAT_POLICY_LABELS: dict[ImageFormatPolicy, str] = {
    IMAGE_FORMAT_POLICY_FOLLOW_GLOBAL: "跟随全局",
    IMAGE_FORMAT_POLICY_RESPECT_REQUEST: "尊重请求",
    IMAGE_FORMAT_POLICY_FORCE_PNG: "强制 PNG",
    IMAGE_FORMAT_POLICY_FORCE_WEBP: "强制 WebP",
}

IMAGE_FORMAT_POLICY_CHOICES = [
    {"value": value, "label": IMAGE_FORMAT_POLICY_LABELS[value]}
    for value in IMAGE_FORMAT_POLICY_VALUES
]


def normalize_image_format_policy(value: object) -> ImageFormatPolicy:
    policy = str(value or DEFAULT_IMAGE_FORMAT_POLICY).strip()
    if policy in IMAGE_FORMAT_POLICY_VALUES:
        return policy  # type: ignore[return-value]
    raise ValueError(f"Unknown image_format_policy: {policy}")


def image_format_policy_label(value: object) -> str:
    return IMAGE_FORMAT_POLICY_LABELS[normalize_image_format_policy(value)]


def effective_image_format_config(
    policy: object,
    global_config: ImageFormatConfig,
) -> ImageFormatConfig:
    normalized = normalize_image_format_policy(policy)
    if normalized == IMAGE_FORMAT_POLICY_FOLLOW_GLOBAL:
        return global_config
    if normalized == IMAGE_FORMAT_POLICY_RESPECT_REQUEST:
        return ImageFormatConfig(mode="request", format=global_config.format)
    if normalized == IMAGE_FORMAT_POLICY_FORCE_PNG:
        return ImageFormatConfig(mode="force", format="png")
    if normalized == IMAGE_FORMAT_POLICY_FORCE_WEBP:
        return ImageFormatConfig(mode="force", format="webp")
    raise ValueError(f"Unknown image_format_policy: {normalized}")
