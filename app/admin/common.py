from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from fastapi.templating import Jinja2Templates

from ..timezones import DISPLAY_TIMEZONE


templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
ALLOWED_ENDPOINT_CHOICES = {
    "generate-image": "图像生成",
    "suggest-tags": "标签建议",
    "upscale": "图片放大",
    "augment-image": "图像增强",
    "encode-vibe": "Vibe 编码",
}
DEFAULT_ALLOWED_ENDPOINTS = "generate-image"


def row_to_dict(row):
    return {key: row[key] for key in row.keys()}


def user_row_to_dict(row):
    data = row_to_dict(row)
    data["allowed_endpoints_list"] = parse_allowed_endpoints(data.get("allowed_endpoints"))
    data["allowed_endpoint_labels"] = [
        ALLOWED_ENDPOINT_CHOICES.get(endpoint, endpoint)
        for endpoint in data["allowed_endpoints_list"]
    ]
    data["allowed_upstreams_list"] = parse_allowed_upstreams(data.get("allowed_upstreams"))
    return data


def parse_allowed_endpoints(value: str | None) -> list[str]:
    if not value:
        return [DEFAULT_ALLOWED_ENDPOINTS]
    endpoints = [item.strip() for item in value.split(",") if item.strip()]
    return endpoints or [DEFAULT_ALLOWED_ENDPOINTS]


def serialize_allowed_endpoints(value: list[str] | None) -> str:
    if not value:
        return DEFAULT_ALLOWED_ENDPOINTS
    valid = []
    for endpoint in value:
        endpoint = endpoint.strip()
        if endpoint in ALLOWED_ENDPOINT_CHOICES and endpoint not in valid:
            valid.append(endpoint)
    return ",".join(valid or [DEFAULT_ALLOWED_ENDPOINTS])


def parse_allowed_upstreams(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def serialize_allowed_upstreams(value: list[str] | None) -> str | None:
    if not value:
        return None
    valid = []
    for upstream_id in value:
        upstream_id = upstream_id.strip()
        if upstream_id and upstream_id not in valid:
            valid.append(upstream_id)
    return ",".join(valid) if valid else None


def usage_log_to_dict(row):
    data = row_to_dict(row)
    data["created_at_display"] = format_display_time(data.get("created_at"))
    data["completed_at_display"] = format_display_time(data.get("completed_at"))
    data["total_ms_display"] = format_duration_ms(data.get("total_ms"))
    data["upstream_ms_display"] = format_duration_ms(data.get("upstream_ms"))
    data["request_payload"] = json_or_none(data.get("request_payload"))
    data["output_files"] = json_or_empty_list(data.get("output_files"))
    data["image_urls"] = json_or_empty_list(data.get("image_urls"))
    return data


def format_display_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S UTC+8")


def format_duration_ms(value) -> str:
    if value is None:
        return "-"
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return "-"
    if milliseconds < 0:
        return "-"
    if milliseconds < 1000:
        return f"{milliseconds} ms"
    seconds = milliseconds / 1000
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes = int(seconds // 60)
    remaining_seconds = seconds - minutes * 60
    return f"{minutes} min {remaining_seconds:.1f} s"


def json_or_none(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def json_or_empty_list(value):
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    return loaded if isinstance(loaded, list) else [loaded]


def optional_query_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": "Invalid query parameter"}) from exc


def local_day_range(value: datetime) -> tuple[datetime, datetime]:
    start = value.astimezone(DISPLAY_TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def add_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1, day=1)
    return value.replace(month=value.month + 1, day=1)


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{int(size)} B"
