from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from .allowlists import AllowedEndpoints, AllowedUpstreams
from .database import Database
from .security import hash_api_key


api_key_header = APIKeyHeader(name="Authorization", auto_error=False)


@dataclass(frozen=True)
class UserContext:
    id: int
    name: str
    tier: str
    is_active: bool
    free_small_only: bool
    allowed_endpoints: frozenset[str]
    allowed_upstreams: frozenset[str]


def _extract_bearer(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


async def get_current_user(
    request: Request,
    authorization: str | None = Depends(api_key_header),
) -> UserContext:
    api_key = _extract_bearer(authorization)
    if not api_key:
        raise HTTPException(status_code=401, detail={"message": "Invalid or missing API Key"})

    db: Database = request.app.state.db
    api_key_hash = hash_api_key(api_key)
    row = db.query_one(
        """
        SELECT id, api_key_hash, name, tier, is_active, free_small_only, allowed_endpoints, allowed_upstreams, deleted_at
        FROM users
        WHERE api_key_hash = ?
        """,
        (api_key_hash,),
    )
    if row is None or not secrets.compare_digest(row["api_key_hash"], api_key_hash):
        raise HTTPException(status_code=401, detail={"message": "Invalid or missing API Key"})
    if row["deleted_at"] is not None:
        raise HTTPException(status_code=401, detail={"message": "Invalid or missing API Key"})
    if not bool(row["is_active"]):
        raise HTTPException(status_code=403, detail={"message": "Account disabled"})
    return UserContext(
        id=int(row["id"]),
        name=str(row["name"]),
        tier=str(row["tier"]),
        is_active=bool(row["is_active"]),
        free_small_only=bool(row["free_small_only"]),
        allowed_endpoints=AllowedEndpoints.parse(row["allowed_endpoints"]).as_frozenset(),
        allowed_upstreams=AllowedUpstreams.parse(row["allowed_upstreams"]).as_frozenset(),
    )
