from __future__ import annotations

import json
import logging
import re
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import ImageConversionConfig, LoggingConfig


LOGGER_NAME = "novelai_proxy"
logger = logging.getLogger(LOGGER_NAME)
SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key"}


def configure_logging(config: LoggingConfig) -> None:
    log_dir = Path(config.directory)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / config.request_log_file

    level = getattr(logging, config.level.upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(level)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)
    logger.info("logging configured", extra={"log_path": str(log_path)})


def dump_model_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return json.loads(json.dumps(value, default=str))


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started_at = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.exception(
                "http request failed method=%s path=%s elapsed_ms=%s",
                scope["method"],
                scope["path"],
                elapsed_ms,
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.info(
                "http request completed method=%s path=%s status=%s elapsed_ms=%s",
                scope["method"],
                scope["path"],
                status_code,
                elapsed_ms,
            )
            logger.debug(
                "http request details method=%s path=%s query=%s client=%s headers=%s",
                scope["method"],
                scope["path"],
                scope.get("query_string", b"").decode("latin-1"),
                _client_addr(scope),
                json_dumps(_safe_headers(scope)),
            )


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
        with zipfile.ZipFile(BytesIO(zip_payload)) as zip_file:
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


def convert_zip_images(zip_payload: bytes, config: ImageConversionConfig) -> bytes:
    if not config.enabled:
        return zip_payload

    try:
        with zipfile.ZipFile(BytesIO(zip_payload)) as source_zip:
            output = BytesIO()
            with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as target_zip:
                for member in source_zip.infolist():
                    data = source_zip.read(member)
                    if member.is_dir():
                        target_zip.writestr(member, data)
                        continue
                    converted = _convert_image_bytes(data, config)
                    if converted is None:
                        target_zip.writestr(member, data)
                        continue
                    target_zip.writestr(_converted_filename(member.filename, config.format), converted)
            return output.getvalue()
    except zipfile.BadZipFile:
        converted = _convert_image_bytes(zip_payload, config)
        return converted if converted is not None else zip_payload


def _convert_image_bytes(data: bytes, config: ImageConversionConfig) -> bytes | None:
    try:
        with Image.open(BytesIO(data)) as image:
            output = BytesIO()
            save_kwargs: dict[str, Any] = {"quality": config.quality, "optimize": True}
            if config.format == "jpeg":
                converted_image = _flatten_for_jpeg(image)
                converted_image.save(output, format="JPEG", **save_kwargs)
            else:
                converted_image = image.copy()
                converted_image.save(output, format="WEBP", **save_kwargs)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def _flatten_for_jpeg(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _converted_filename(filename: str, target_format: str) -> str:
    suffix = ".jpg" if target_format == "jpeg" else ".webp"
    path = Path(filename)
    stem = path.name[: -len(path.suffix)] if path.suffix else path.name
    converted_name = f"{stem}{suffix}"
    return str(path.with_name(converted_name)).replace("\\", "/")


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        return suffix
    return ".bin"


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "image"


def _client_addr(scope: Scope) -> str:
    client = scope.get("client")
    if not client:
        return ""
    return f"{client[0]}:{client[1]}"


def _safe_headers(scope: Scope) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin-1").lower()
        if name in SENSITIVE_HEADERS:
            headers[name] = "[redacted]"
        else:
            headers[name] = raw_value.decode("latin-1")
    return headers
