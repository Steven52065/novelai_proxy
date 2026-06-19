from __future__ import annotations

import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol

import httpx
from PIL import Image

from .config import ImageHostingConfig
from .logging_utils import logger


IMAGE_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

CONVERSION_FORMATS = {
    "png": {"suffix": ".png", "content_type": "image/png", "pillow_format": "PNG"},
    "jpeg": {"suffix": ".jpg", "content_type": "image/jpeg", "pillow_format": "JPEG"},
    "webp": {"suffix": ".webp", "content_type": "image/webp", "pillow_format": "WEBP"},
}


@dataclass(frozen=True)
class ImageUploadFile:
    filename: str
    content: bytes
    content_type: str


class ImageHostClient(Protocol):
    provider: str

    async def upload_image(self, image: ImageUploadFile) -> str:
        ...


class CatboxImageHost:
    provider = "catbox"

    def __init__(self, *, api_url: str, userhash: str, timeout_seconds: float):
        userhash = userhash.strip()
        if not userhash:
            raise ValueError("Catbox userhash is required when image hosting is enabled")
        self.api_url = api_url
        self.userhash = userhash
        self.timeout_seconds = timeout_seconds

    async def upload_image(self, image: ImageUploadFile) -> str:
        data = {"reqtype": "fileupload", "userhash": self.userhash}
        files = {
            "fileToUpload": (
                image.filename,
                image.content,
                image.content_type,
            )
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(self.api_url, data=data, files=files)
        response.raise_for_status()
        url = response.text.strip()
        if not url.startswith(("http://", "https://")):
            raise ImageHostUploadError(url or "empty image host response")
        return url


class ImageHostingService:
    def __init__(self, config: ImageHostingConfig):
        self.config = config
        self.max_pending_uploads = config.max_pending_uploads
        self.client = self._build_client(config) if config.enabled else None

    async def upload_zip_images(self, *, zip_payload: bytes, request_id: str) -> list[dict[str, object]]:
        if self.client is None:
            return []
        uploads = []
        for index, source_image in enumerate(_extract_images(zip_payload), start=1):
            try:
                image = self._prepare_upload_image(source_image)
            except Exception:
                logger.exception(
                    "image local format conversion failed request_id=%s filename=%s target_format=%s",
                    request_id,
                    source_image.filename,
                    self.config.local_conversion_format,
                )
                continue
            try:
                url = await self.client.upload_image(image)
            except Exception:
                logger.exception(
                    "image host upload failed request_id=%s provider=%s filename=%s",
                    request_id,
                    self.client.provider,
                    image.filename,
                )
                continue
            uploads.append(
                {
                    "provider": self.client.provider,
                    "url": url,
                    "filename": image.filename,
                    "bytes": len(image.content),
                    "index": index,
                }
            )
        return uploads

    def _prepare_upload_image(self, image: ImageUploadFile) -> ImageUploadFile:
        if not self.config.local_format_conversion:
            return image
        if _image_matches_target_format(image, self.config.local_conversion_format):
            return image
        return _convert_image(image, self.config.local_conversion_format)

    @staticmethod
    def _build_client(config: ImageHostingConfig) -> ImageHostClient:
        if config.provider == "catbox":
            return CatboxImageHost(
                api_url=config.catbox.api_url,
                userhash=config.catbox.userhash,
                timeout_seconds=config.timeout_seconds,
            )
        raise ValueError(f"Unsupported image host provider: {config.provider}")


class ImageHostUploadError(Exception):
    pass


def _convert_image(image: ImageUploadFile, target_format: str) -> ImageUploadFile:
    target = CONVERSION_FORMATS[target_format]
    with Image.open(BytesIO(image.content)) as source:
        converted = _prepare_pillow_image(source, target_format)
        output = BytesIO()
        save_kwargs: dict[str, object] = {}
        if target_format == "webp":
            save_kwargs["lossless"] = True
        elif target_format == "jpeg":
            save_kwargs["quality"] = 95
        converted.save(output, format=target["pillow_format"], **save_kwargs)

    filename = f"{Path(image.filename).stem}{target['suffix']}"
    return ImageUploadFile(
        filename=filename,
        content=output.getvalue(),
        content_type=target["content_type"],
    )


def _image_matches_target_format(image: ImageUploadFile, target_format: str) -> bool:
    target = CONVERSION_FORMATS[target_format]
    if image.content_type != target["content_type"]:
        return False
    with Image.open(BytesIO(image.content)) as source:
        return source.format == target["pillow_format"]


def _prepare_pillow_image(image: Image.Image, target_format: str) -> Image.Image:
    if target_format == "jpeg":
        if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            return background
        if image.mode != "RGB":
            return image.convert("RGB")
    return image.copy()


def _extract_images(zip_payload: bytes) -> list[ImageUploadFile]:
    images: list[ImageUploadFile] = []
    try:
        with zipfile.ZipFile(BytesIO(zip_payload)) as zip_file:
            for member in zip_file.infolist():
                if member.is_dir():
                    continue
                suffix = Path(member.filename).suffix.lower()
                content_type = IMAGE_CONTENT_TYPES.get(suffix)
                if content_type is None:
                    continue
                data = zip_file.read(member)
                if not data:
                    continue
                images.append(
                    ImageUploadFile(
                        filename=Path(member.filename).name,
                        content=data,
                        content_type=content_type,
                    )
                )
    except zipfile.BadZipFile:
        return []
    return images
