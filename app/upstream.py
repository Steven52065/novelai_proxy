from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode
from zipfile import ZipFile

from curl_cffi.requests import AsyncSession

from .api_errors import APIError, AuthError, DataSerializationError
from .logging_utils import dump_model_payload
from .novelai_endpoints import (
    AUGMENT_IMAGE_ENDPOINT,
    ENCODE_VIBE_ENDPOINT,
    GENERATE_IMAGE_ENDPOINT,
    SUGGEST_TAGS_ENDPOINT,
    UPSCALE_ENDPOINT,
)
from .novelai_models import AugmentImageRequest, UpscaleRequest


class _Credential(Protocol):
    async def get_session(self, timeout: int = 180, update_headers: dict = None):
        ...


_CORRELATION_ID = uuid.uuid4().hex[:6]


@dataclass
class _ApiKeySessionFactory:
    """Create authenticated curl-cffi sessions for API and JWT tokens."""

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

    async def upscale_zip(self, req: UpscaleRequest) -> bytes:
        payload = dump_model_payload(req)
        return await self._post_zip(UPSCALE_ENDPOINT, payload)

    async def augment_image_zip(self, req: AugmentImageRequest) -> bytes:
        payload = dump_model_payload(req)
        return await self._post_zip(AUGMENT_IMAGE_ENDPOINT, payload)

    async def suggest_tags(self, model: str, prompt: str, lang: str = "en") -> dict[str, Any]:
        params = {"model": model, "prompt": prompt, "lang": lang}
        return await self._get_json(f"{SUGGEST_TAGS_ENDPOINT}?{urlencode(params)}", request=params)

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
                _raise_response_error(response, payload)
            if not response.content:
                raise DataSerializationError(
                    "The upstream response is empty.",
                    request=payload,
                    response={},
                    code=response.status_code,
                )
            return response.content

    async def _post_zip(self, url: str, payload: dict[str, Any]) -> bytes:
        response_content = await self._post_binary(url, payload)
        _validate_zip_response(response_content, request=payload)
        return response_content

    async def _get_json(self, url: str, *, request: dict[str, Any]) -> dict[str, Any]:
        async with await self._credential().get_session() as sess:
            response = await sess.get(url)
            if response.status_code != 200:
                _raise_response_error(response, request)
            try:
                body = response.json()
            except Exception as exc:
                raise DataSerializationError(
                    "The upstream response is not valid JSON.",
                    request=request,
                    response={},
                    code=response.status_code,
                ) from exc
            if not isinstance(body, dict):
                raise DataSerializationError(
                    "The upstream response has an unexpected JSON shape.",
                    request=request,
                    response=body,
                    code=response.status_code,
                )
            return body


def _utc_initiated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_zip_response(content: bytes, *, request: dict[str, Any]) -> None:
    """Validate a tool response without changing the ZIP returned downstream."""
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            has_nonempty_file = False
            for member in members:
                if member.is_dir():
                    continue
                if archive.read(member):
                    has_nonempty_file = True
            if not has_nonempty_file:
                raise DataSerializationError(
                    "The ZIP response contains no files.",
                    request=request,
                    response={},
                    code="201",
                )
    except DataSerializationError:
        raise
    except Exception as exc:
        raise DataSerializationError(
            "Invalid ZIP file received from the API.",
            request=request,
            response={},
            code="201",
        ) from exc


_MAX_ERROR_BODY_CHARS = 2000


def _response_error(response) -> dict[str, Any]:
    try:
        body = response.json()
        if isinstance(body, dict):
            return body
    except Exception:
        pass
    content = response.content
    text = content.decode("utf-8", errors="replace").strip()
    # 非 JSON 的上游错误（网关 HTML 页、纯文本）过去在超过 200 字节时被整体丢弃，
    # 只留下一句 "Response content too long"，排查 500 时没有任何信息量。
    # 改为保留开头片段并标注截断长度。
    if len(text) > _MAX_ERROR_BODY_CHARS:
        text = f"{text[:_MAX_ERROR_BODY_CHARS]}...[truncated, {len(text)} chars total]"
    return {"statusCode": response.status_code, "message": text or "Upstream request failed"}


def _raise_response_error(response, request: dict[str, Any]) -> None:
    error = _response_error(response)
    exc_type = AuthError if response.status_code in {400, 401, 402} else APIError
    raise exc_type(
        error.get("message", "Upstream request failed"),
        request=request,
        response=error,
        code=response.status_code,
    )
