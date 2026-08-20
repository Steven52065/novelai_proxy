from __future__ import annotations

from datetime import datetime, timezone
import json
import io
import uuid
import zipfile
from dataclasses import dataclass
from typing import Any, Protocol

from novelai_python.sdk.ai.augment_image import AugmentImageInfer
from novelai_python.sdk.ai.generate_image.suggest_tags import SuggestTags
from novelai_python.sdk.ai.upscale import Upscale
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random
from curl_cffi.requests import AsyncSession

from .api_errors import APIError, AuthError, DataSerializationError
from .novelai_endpoints import ENCODE_VIBE_ENDPOINT, GENERATE_IMAGE_ENDPOINT


class _Credential(Protocol):
    async def get_session(self, timeout: int = 180, update_headers: dict = None):
        ...


_CORRELATION_ID = uuid.uuid4().hex[:6]


@dataclass
class _ApiKeySessionFactory:
    """Small replacement for the SDK's Api/JwtCredential classes."""

    token: str
    token_kind: str
    x_correlation_id: str = _CORRELATION_ID

    async def get_session(self, timeout: int = 180, update_headers: dict | None = None):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "x-correlation-id": self.x_correlation_id,
            "x-initiated-at": _utc_initiated_at(),
        }
        if update_headers:
            if not isinstance(update_headers, dict):
                raise AssertionError("update_headers must be a dict")
            headers.update(update_headers)
        return AsyncSession(timeout=timeout, headers=headers, impersonate="chrome136")


class UpstreamClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._credential_instance = self._api_key_credential(api_key)

    def _credential(self) -> _Credential:
        return self._credential_instance

    @staticmethod
    def _api_key_credential(api_key: str) -> _ApiKeySessionFactory:
        token = api_key.strip()
        if token.startswith("ey"):
            return _ApiKeySessionFactory(token=token, token_kind="jwt")
        return _ApiKeySessionFactory(token=token, token_kind="api")

    async def generate_image_payload_zip(self, payload: dict[str, Any]) -> bytes:
        return await self._post_binary(GENERATE_IMAGE_ENDPOINT, payload)

    async def encode_vibe_binary(self, payload: dict[str, Any]) -> bytes:
        return await self._post_binary(ENCODE_VIBE_ENDPOINT, payload)

    async def post_binary(self, url: str, payload: dict[str, Any]) -> bytes:
        return await self._post_binary(url, payload)

    async def upscale_zip(self, req: Upscale) -> bytes:
        result = await req.request(session=self._credential())
        files = [result.files] if result.files else []
        return _files_to_zip(files)

    async def augment_image_zip(self, req: AugmentImageInfer) -> bytes:
        result = await req.request(session=self._credential())
        return _files_to_zip(result.files or [])

    async def suggest_tags(self, model: str, prompt: str, lang: str = "en") -> dict[str, Any]:
        req = SuggestTags(model=model, prompt=prompt, lang=lang)
        result = await req.request(session=self._credential())
        return result.model_dump()

    @retry(
        wait=wait_random(min=1, max=3),
        stop=stop_after_attempt(3),
        retry=retry_if_exception(lambda exc: isinstance(exc, APIError) and str(exc.code) == "500"),
        reraise=True,
    )
    async def _post_binary(self, url: str, payload: dict[str, Any]) -> bytes:
        async with await self._credential().get_session() as sess:
            response = await sess.post(url, data=json.dumps(payload).encode("utf-8"))
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if response.status_code >= 400 or content_type not in {
                "application/zip",
                "binary/octet-stream",
                "application/binary",
                "application/octet-stream",
                "application/x-zip-compressed",
            }:
                error = _response_error(response)
                exc_type = AuthError if response.status_code in {400, 401, 402} else APIError
                raise exc_type(
                    error.get("message", "Upstream request failed"),
                    request=payload,
                    response=error,
                    code=response.status_code,
                )
            if not response.content:
                raise DataSerializationError(
                    "The upstream response is empty.",
                    request=payload,
                    response={},
                    code=response.status_code,
                )
            return response.content


def _utc_initiated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _files_to_zip(files: list[tuple[str, bytes]] | tuple[tuple[str, bytes], ...]) -> bytes:
    zip_file_bytes = io.BytesIO()
    with zipfile.ZipFile(zip_file_bytes, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for filename, data in files:
            zip_file.writestr(zinfo_or_arcname=filename, data=data)
    return zip_file_bytes.getvalue()


def _response_error(response) -> dict[str, Any]:
    try:
        body = response.json()
        if isinstance(body, dict):
            return body
    except Exception:
        pass
    content = response.content
    message = content.decode("utf-8", errors="replace") if len(content) <= 200 else "Response content too long"
    return {"statusCode": response.status_code, "message": message}
