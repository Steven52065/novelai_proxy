from __future__ import annotations

import hashlib
import secrets


def generate_api_key() -> str:
    return f"nai_proxy_{secrets.token_urlsafe(32)}"


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def constant_time_equal(left: str, right: str) -> bool:
    return secrets.compare_digest(left, right)
