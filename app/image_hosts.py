from __future__ import annotations

import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol

import httpx

from .config import ImageHostingConfig
from .logging_utils import logger


IMAGE_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
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

    def __init__(self, *, api_url: str, userhash: str | None, timeout_seconds: float):
        self.api_url = api_url
        self.userhash = userhash
        self.timeout_seconds = timeout_seconds

    async def upload_image(self, image: ImageUploadFile) -> str:
        data = {"reqtype": "fileupload"}
        if self.userhash:
            data["userhash"] = self.userhash
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
        self.client = self._build_client(config) if config.enabled else None

    async def upload_zip_images(self, *, zip_payload: bytes, request_id: str) -> list[dict[str, object]]:
        if self.client is None:
            return []
        uploads = []
        for index, image in enumerate(_extract_images(zip_payload), start=1):
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
