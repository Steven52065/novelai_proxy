from __future__ import annotations

import asyncio
import logging
import zipfile
from io import BytesIO

import httpx
import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from app.config import CatboxConfig, ImageHostingConfig
from app.image_hosts import CatboxImageHost, ImageHostingService, ImageHostUploadError, ImageUploadFile
from app.logging_utils import LOGGER_NAME


class RecordingImageHostClient:
    provider = "catbox"

    def __init__(self):
        self.uploaded_images: list[ImageUploadFile] = []

    async def upload_image(self, image: ImageUploadFile) -> str:
        self.uploaded_images.append(image)
        return f"https://files.catbox.moe/{image.filename}"


class FailingImageHostClient:
    provider = "catbox"

    async def upload_image(self, image: ImageUploadFile) -> str:
        raise RuntimeError("upload failed")


def test_catbox_upload_logs_error_response_without_userhash(monkeypatch, caplog):
    userhash = "secret-catbox-userhash"
    response_body = f"invalid userhash: {userhash}\n" + ("x" * 2100)

    def handle_request(request: httpx.Request) -> httpx.Response:
        assert userhash.encode() in request.content
        return httpx.Response(
            400,
            headers={"content-type": "text/plain; charset=utf-8"},
            text=response_body,
            request=request,
        )

    real_async_client = httpx.AsyncClient

    def build_client(*, timeout):
        return real_async_client(timeout=timeout, transport=httpx.MockTransport(handle_request))

    monkeypatch.setattr("app.image_hosts.httpx.AsyncClient", build_client)
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    host = CatboxImageHost(
        api_url="https://catbox.moe/user/api.php",
        userhash=userhash,
        timeout_seconds=30,
    )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            host.upload_image(
                ImageUploadFile(
                    filename="image_0.webp",
                    content=b"fake-webp-content",
                    content_type="image/webp",
                )
            )
        )

    assert "status_code=400" in caplog.text
    assert "response_content_type=text/plain; charset=utf-8" in caplog.text
    assert "invalid userhash: [redacted]" in caplog.text
    assert "[truncated " in caplog.text
    assert "filename=image_0.webp" in caplog.text
    assert "bytes=17" in caplog.text
    assert "upload_content_type=image/webp" in caplog.text
    assert userhash not in caplog.text


def test_catbox_upload_logs_invalid_success_response_without_userhash(monkeypatch, caplog):
    userhash = "secret-catbox-userhash"

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text=f"upload rejected for {userhash}",
            request=request,
        )

    real_async_client = httpx.AsyncClient

    def build_client(*, timeout):
        return real_async_client(timeout=timeout, transport=httpx.MockTransport(handle_request))

    monkeypatch.setattr("app.image_hosts.httpx.AsyncClient", build_client)
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    host = CatboxImageHost(
        api_url="https://catbox.moe/user/api.php",
        userhash=userhash,
        timeout_seconds=30,
    )

    with pytest.raises(ImageHostUploadError, match="invalid image host response") as exc_info:
        asyncio.run(
            host.upload_image(
                ImageUploadFile(
                    filename="image.png",
                    content=b"fake-png-content",
                    content_type="image/png",
                )
            )
        )

    assert userhash not in str(exc_info.value)
    assert "image host invalid response" in caplog.text
    assert "response_body='upload rejected for [redacted]'" in caplog.text
    assert userhash not in caplog.text


def test_image_host_upload_failure_logs_file_details(caplog):
    image_bytes = _png_bytes()
    service = ImageHostingService(
        ImageHostingConfig(
            enabled=True,
            catbox=CatboxConfig(userhash="test-userhash"),
        )
    )
    service.client = FailingImageHostClient()
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)

    uploads = asyncio.run(
        service.upload_zip_images(
            zip_payload=_image_zip("image.png", image_bytes),
            request_id="failed-request",
        )
    )

    assert uploads == []
    assert "request_id=failed-request" in caplog.text
    assert "filename=image.png" in caplog.text
    assert f"bytes={len(image_bytes)}" in caplog.text
    assert "content_type=image/png" in caplog.text


def test_image_host_upload_converts_image_locally_when_enabled():
    zip_payload = _image_zip("image.png", _png_bytes())
    client = RecordingImageHostClient()
    service = ImageHostingService(
        ImageHostingConfig(
            enabled=True,
            local_format_conversion=True,
            local_conversion_format="webp",
            catbox=CatboxConfig(userhash="test-userhash"),
        )
    )
    service.client = client

    uploads = asyncio.run(service.upload_zip_images(zip_payload=zip_payload, request_id="convert-request"))

    assert len(client.uploaded_images) == 1
    uploaded_image = client.uploaded_images[0]
    assert uploaded_image.filename == "image.webp"
    assert uploaded_image.content_type == "image/webp"
    assert Image.open(BytesIO(uploaded_image.content)).format == "WEBP"
    assert uploads == [
        {
            "provider": "catbox",
            "url": "https://files.catbox.moe/image.webp",
            "filename": "image.webp",
            "bytes": len(uploaded_image.content),
            "index": 1,
        }
    ]

    with zipfile.ZipFile(BytesIO(zip_payload)) as zip_file:
        assert zip_file.namelist() == ["image.png"]


def test_image_host_upload_skips_local_conversion_when_format_already_matches():
    image_bytes = _png_bytes_with_metadata()
    client = RecordingImageHostClient()
    service = ImageHostingService(
        ImageHostingConfig(
            enabled=True,
            local_format_conversion=True,
            local_conversion_format="png",
            catbox=CatboxConfig(userhash="test-userhash"),
        )
    )
    service.client = client

    uploads = asyncio.run(service.upload_zip_images(zip_payload=_image_zip("image.png", image_bytes), request_id="same-format-request"))

    assert client.uploaded_images == [
        ImageUploadFile(
            filename="image.png",
            content=image_bytes,
            content_type="image/png",
        )
    ]
    assert uploads == [
        {
            "provider": "catbox",
            "url": "https://files.catbox.moe/image.png",
            "filename": "image.png",
            "bytes": len(image_bytes),
            "index": 1,
        }
    ]


def test_image_host_upload_keeps_original_image_format_by_default():
    image_bytes = _png_bytes()
    client = RecordingImageHostClient()
    service = ImageHostingService(
        ImageHostingConfig(
            enabled=True,
            catbox=CatboxConfig(userhash="test-userhash"),
        )
    )
    service.client = client

    asyncio.run(service.upload_zip_images(zip_payload=_image_zip("image.png", image_bytes), request_id="original-request"))

    assert client.uploaded_images == [
        ImageUploadFile(
            filename="image.png",
            content=image_bytes,
            content_type="image/png",
        )
    ]


def _png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", (2, 2), (255, 0, 0, 128)).save(buffer, format="PNG")
    return buffer.getvalue()


def _png_bytes_with_metadata() -> bytes:
    buffer = BytesIO()
    metadata = PngInfo()
    metadata.add_text("novelai_proxy_test_marker", "preserve-original-bytes")
    Image.new("RGBA", (2, 2), (255, 0, 0, 128)).save(buffer, format="PNG", pnginfo=metadata)
    return buffer.getvalue()


def _image_zip(filename: str, content: bytes) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as zip_file:
        zip_file.writestr(filename, content)
    return buffer.getvalue()
