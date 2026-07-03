from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path


IMAGE_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def zip_images_to_data_urls(zip_payload: bytes) -> list[dict[str, str | int]]:
    images: list[dict[str, str | int]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_payload)) as zip_file:
            for member in zip_file.infolist():
                if member.is_dir():
                    continue
                content_type = IMAGE_CONTENT_TYPES.get(Path(member.filename).suffix.lower())
                if content_type is None:
                    continue
                data = zip_file.read(member)
                if not data:
                    continue
                images.append(
                    {
                        "filename": member.filename,
                        "content_type": content_type,
                        "bytes": len(data),
                        "data_url": f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}",
                    }
                )
    except zipfile.BadZipFile:
        return []
    return images
