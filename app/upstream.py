from __future__ import annotations

import json
import io
import zipfile
from typing import Any, Protocol

from novelai_python._exceptions import APIError, AuthError, DataSerializationError
from novelai_python.credential import ApiCredential, JwtCredential, SecretStr
from novelai_python.sdk.ai.augment_image import AugmentImageInfer
from novelai_python.sdk.ai.generate_image import GenerateImageInfer
from novelai_python.sdk.ai.generate_image.suggest_tags import SuggestTags
from novelai_python.sdk.ai.upscale import Upscale

from .novelai_endpoints import ENCODE_VIBE_ENDPOINT, GENERATE_IMAGE_ENDPOINT


class _Credential(Protocol):
    async def get_session(self, timeout: int = 180, update_headers: dict = None):
        ...


class UpstreamClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._credential_instance = self._api_key_credential(api_key)

    @classmethod
    def from_config(cls, config) -> "UpstreamClient":
        return cls(config.api_key)

    def _credential(self) -> _Credential:
        return self._credential_instance

    @staticmethod
    def _api_key_credential(api_key: str) -> ApiCredential | JwtCredential:
        token = api_key.strip()
        if token.startswith("ey"):
            return JwtCredential(jwt_token=SecretStr(token))
        return ApiCredential(api_token=SecretStr(token))

    async def generate_image_zip(self, req: GenerateImageInfer) -> bytes:
        result = await req.request(session=self._credential())
        return _files_to_zip(result.files or [])

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

    async def _post_binary(self, url: str, payload: dict[str, Any]) -> bytes:
        async with await self._credential().get_session() as sess:
            response = await sess.post(url, data=json.dumps(payload).encode("utf-8"))
            content_type = response.headers.get("Content-Type", "")
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
