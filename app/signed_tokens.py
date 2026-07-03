from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


def sign_payload(payload: dict[str, Any], secret: str) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
    signature = _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def verify_payload(token: str | None, secret: str) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    try:
        body_bytes = body.encode("ascii")
    except UnicodeEncodeError:
        return None
    expected = _b64(hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        expires_at = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return None
    if expires_at < int(time.time()):
        return None
    return payload


def expiring_payload(seconds: int, **values: Any) -> dict[str, Any]:
    return {"exp": int(time.time()) + seconds, **values}


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")
