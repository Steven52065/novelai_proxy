from __future__ import annotations

import io
import zipfile
from typing import Any

from novelai_python.credential import JwtCredential, SecretStr
from novelai_python.sdk.ai.augment_image import AugmentImageInfer
from novelai_python.sdk.ai.generate_image import GenerateImageInfer
from novelai_python.sdk.ai.generate_image.suggest_tags import SuggestTags
from novelai_python.sdk.ai.upscale import Upscale


class UpstreamClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _credential(self) -> JwtCredential:
        return JwtCredential(jwt_token=SecretStr(self.api_key))

    async def generate_image_zip(self, req: GenerateImageInfer) -> bytes:
        result = await req.request(session=self._credential())
        return _files_to_zip(result.files or [])

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


def _files_to_zip(files: list[tuple[str, bytes]] | tuple[tuple[str, bytes], ...]) -> bytes:
        zip_file_bytes = io.BytesIO()
        with zipfile.ZipFile(zip_file_bytes, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for filename, data in files:
                zip_file.writestr(zinfo_or_arcname=filename, data=data)
        return zip_file_bytes.getvalue()
