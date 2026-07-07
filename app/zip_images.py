from __future__ import annotations

import base64
import io
import re
import zipfile
from pathlib import Path

from .config import LoggingConfig


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


def archive_zip_images(
    *,
    zip_payload: bytes,
    request_id: str,
    action: str,
    config: LoggingConfig,
) -> list[str]:
    if not config.save_generated_images:
        return []

    output_root = Path(config.directory) / config.generated_images_dir / request_id
    output_root.mkdir(parents=True, exist_ok=True)
    saved_files: list[str] = []

    try:
        with zipfile.ZipFile(io.BytesIO(zip_payload)) as zip_file:
            for index, member in enumerate(zip_file.infolist(), start=1):
                if member.is_dir():
                    continue
                data = zip_file.read(member)
                if not data:
                    continue
                ext = _safe_suffix(member.filename)
                filename = f"{index:02d}_{_safe_name(action)}{ext}"
                path = output_root / filename
                path.write_bytes(data)
                saved_files.append(str(path))
    except zipfile.BadZipFile:
        path = output_root / "response.bin"
        path.write_bytes(zip_payload)
        saved_files.append(str(path))

    return saved_files


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        return suffix
    return ".bin"


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "image"
