from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from ..allowlists import ALLOWED_ENDPOINT_CHOICES, AllowedEndpoints, AllowedUpstreams
from ..quota_manager import normalize_reset_day
from ..timezones import DISPLAY_TIMEZONE


def row_to_dict(row):
    return {key: row[key] for key in row.keys()}


def user_row_to_dict(row):
    data = row_to_dict(row)
    data["allowed_endpoints_list"] = AllowedEndpoints.parse(data.get("allowed_endpoints")).as_list()
    data["allowed_endpoint_labels"] = [
        ALLOWED_ENDPOINT_CHOICES.get(endpoint, endpoint)
        for endpoint in data["allowed_endpoints_list"]
    ]
    data["allowed_upstreams_list"] = AllowedUpstreams.parse(data.get("allowed_upstreams")).as_list()
    return data


def upstream_choices(request: Request) -> list[str]:
    clients = getattr(request.app.state, "upstream_clients", None)
    if isinstance(clients, dict) and clients:
        return list(clients.keys())
    return ["default"]


def validate_allowed_endpoints(allowed_endpoints: list[str] | None) -> None:
    if allowed_endpoints is None:
        return
    endpoints = AllowedEndpoints.of(allowed_endpoints)
    if not endpoints.items:
        raise HTTPException(status_code=400, detail={"message": "At least one endpoint must be allowed"})
    unknown = endpoints.unknown()
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"message": f"Unknown endpoint: {', '.join(unknown)}"},
        )


def validate_allowed_upstreams(allowed_upstreams: list[str] | None, request: Request) -> None:
    if not allowed_upstreams:
        return
    valid_upstreams = set(upstream_choices(request))
    unknown = sorted(AllowedUpstreams.of(allowed_upstreams).as_frozenset() - valid_upstreams)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"message": f"Unknown upstream id: {', '.join(unknown)}"},
        )


def validate_free_small_daily_limit(enabled: bool, limit: int) -> None:
    if enabled and int(limit) < 1:
        raise HTTPException(
            status_code=400,
            detail={"message": "free_small_daily_limit must be >= 1 when enabled"},
        )


def normalize_reset_day_or_400(reset_period: str, reset_day: int | None) -> int:
    try:
        return normalize_reset_day(reset_period, reset_day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc


def notify_dashboard_change(request: Request) -> None:
    event_bus = getattr(request.app.state, "dashboard_events", None)
    if event_bus is not None:
        event_bus.notify_nowait()


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
