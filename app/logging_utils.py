from __future__ import annotations

import json
import logging
import re
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from .config import LoggingConfig


LOGGER_NAME = "novelai_proxy"
logger = logging.getLogger(LOGGER_NAME)


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


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        return suffix
    return ".bin"


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "image"
