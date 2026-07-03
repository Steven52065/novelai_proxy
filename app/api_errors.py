from __future__ import annotations

from novelai_python._exceptions import APIError


def api_error_status_code(exc: APIError) -> int:
    return int(exc.code) if str(exc.code or "").isdigit() else 502
