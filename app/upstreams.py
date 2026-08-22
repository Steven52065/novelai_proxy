from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import IntegrityError
from typing import Any, Callable

from .allowlists import AllowedUpstreams
from .config import RESERVED_UPSTREAM_IDS
from .database import Database
from .database.clock import utc_now_iso
from .domain_errors import InvalidDomainInput, UpstreamConflict, UpstreamNotFound
from .queue_models import UpstreamQueueTarget
from .upstream import UpstreamClient


@dataclass(frozen=True)
class NovelAIUpstreamRecord:
    id: str
    api_key: str
    enabled: bool
    created_at: str
    updated_at: str | None


@dataclass(frozen=True)
class NovelAISettings:
    account_tier: int
    upscale_anlas_cost: int
    created_at: str
    updated_at: str | None


def mask_token(token: str) -> str:
    value = token.strip()
    if not value:
        return ""
    if len(value) <= 10:
        return f"{value[:2]}...{value[-2:]}"
    return f"{value[:6]}...{value[-4:]}"


def validate_upstream_id(upstream_id: str) -> str:
    normalized = upstream_id.strip()
    if not normalized:
        raise InvalidDomainInput("上游 id 不能为空")
    if normalized in RESERVED_UPSTREAM_IDS:
        raise InvalidDomainInput(f"上游 id 已被保留：{normalized}")
    return normalized


def validate_api_key(api_key: str) -> str:
    normalized = api_key.strip()
    if not normalized:
        raise InvalidDomainInput("api_key 不能为空")
    return normalized


def upstream_to_public_dict(record: NovelAIUpstreamRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "enabled": record.enabled,
        "api_key_masked": mask_token(record.api_key),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


class NovelAIUpstreamRepository:
    def __init__(self, db: Database):
        self.db = db

    def list(self, *, include_disabled: bool = True) -> list[NovelAIUpstreamRecord]:
        where = "" if include_disabled else "WHERE enabled = 1"
        rows = self.db.query_all(
            f"""
            SELECT id, api_key, enabled, created_at, updated_at
            FROM novelai_upstreams
            {where}
            ORDER BY id
            """
        )
        return [self._row_to_record(row) for row in rows]

    def get(self, upstream_id: str) -> NovelAIUpstreamRecord | None:
        row = self.db.query_one(
            """
            SELECT id, api_key, enabled, created_at, updated_at
            FROM novelai_upstreams
            WHERE id = ?
            """,
            (upstream_id,),
        )
        return self._row_to_record(row) if row is not None else None

    def create(self, *, upstream_id: str, api_key: str, enabled: bool = True) -> NovelAIUpstreamRecord:
        upstream_id = validate_upstream_id(upstream_id)
        api_key = validate_api_key(api_key)
        timestamp = utc_now_iso()
        try:
            self.db.execute(
                """
                INSERT INTO novelai_upstreams(id, api_key, enabled, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (upstream_id, api_key, 1 if enabled else 0, timestamp),
            )
        except IntegrityError as exc:
            raise UpstreamConflict(f"上游 id 已存在：{upstream_id}") from exc
        created = self.get(upstream_id)
        assert created is not None
        return created

    def update(
        self,
        upstream_id: str,
        *,
        api_key: str | None = None,
        enabled: bool | None = None,
    ) -> NovelAIUpstreamRecord:
        existing = self.get(upstream_id)
        if existing is None:
            raise UpstreamNotFound()

        fields: list[str] = []
        params: list[Any] = []
        if api_key is not None:
            fields.append("api_key = ?")
            params.append(validate_api_key(api_key))
        if enabled is not None:
            fields.append("enabled = ?")
            params.append(1 if enabled else 0)
        if not fields:
            return existing

        fields.append("updated_at = ?")
        params.append(utc_now_iso())
        params.append(upstream_id)
        self.db.execute(
            f"UPDATE novelai_upstreams SET {', '.join(fields)} WHERE id = ?",
            tuple(params),
        )
        updated = self.get(upstream_id)
        assert updated is not None
        return updated

    def delete(self, upstream_id: str) -> None:
        if self.get(upstream_id) is None:
            raise UpstreamNotFound()
        conflicts = self.find_allowed_upstream_references(upstream_id)
        if conflicts:
            raise UpstreamConflict(
                "该上游仍被用户或用户组引用",
                details={"references": conflicts},
            )
        self.db.execute("DELETE FROM novelai_upstreams WHERE id = ?", (upstream_id,))

    def find_allowed_upstream_references(self, upstream_id: str) -> dict[str, list[int]]:
        user_rows = self.db.query_all("SELECT id, allowed_upstreams FROM users WHERE deleted_at IS NULL")
        group_rows = self.db.query_all("SELECT id, default_allowed_upstreams FROM user_groups")
        user_ids = [
            int(row["id"])
            for row in user_rows
            if upstream_id in AllowedUpstreams.parse(row["allowed_upstreams"]).as_frozenset()
        ]
        group_ids = [
            int(row["id"])
            for row in group_rows
            if upstream_id in AllowedUpstreams.parse(row["default_allowed_upstreams"]).as_frozenset()
        ]
        result: dict[str, list[int]] = {}
        if user_ids:
            result["users"] = user_ids
        if group_ids:
            result["groups"] = group_ids
        return result

    def get_settings(self) -> NovelAISettings:
        row = self.db.query_one(
            """
            SELECT account_tier, upscale_anlas_cost, created_at, updated_at
            FROM novelai_settings
            WHERE id = 1
            """
        )
        if row is None:
            timestamp = utc_now_iso()
            self.db.execute(
                """
                INSERT INTO novelai_settings(id, account_tier, upscale_anlas_cost, created_at)
                VALUES (1, 3, 0, ?)
                """,
                (timestamp,),
            )
            row = self.db.query_one(
                """
                SELECT account_tier, upscale_anlas_cost, created_at, updated_at
                FROM novelai_settings
                WHERE id = 1
                """
            )
        assert row is not None
        return NovelAISettings(
            account_tier=int(row["account_tier"]),
            upscale_anlas_cost=int(row["upscale_anlas_cost"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def update_settings(
        self,
        *,
        account_tier: int | None = None,
        upscale_anlas_cost: int | None = None,
    ) -> NovelAISettings:
        fields: list[str] = []
        params: list[Any] = []
        if account_tier is not None:
            if account_tier < 0 or account_tier > 3:
                raise InvalidDomainInput("account_tier 必须在 0 到 3 之间")
            fields.append("account_tier = ?")
            params.append(int(account_tier))
        if upscale_anlas_cost is not None:
            if upscale_anlas_cost < 0:
                raise InvalidDomainInput("upscale_anlas_cost 必须大于等于 0")
            fields.append("upscale_anlas_cost = ?")
            params.append(int(upscale_anlas_cost))
        if not fields:
            return self.get_settings()

        fields.append("updated_at = ?")
        params.append(utc_now_iso())
        self.db.execute(
            f"UPDATE novelai_settings SET {', '.join(fields)} WHERE id = 1",
            tuple(params),
        )
        return self.get_settings()

    @staticmethod
    def _row_to_record(row) -> NovelAIUpstreamRecord:
        return NovelAIUpstreamRecord(
            id=row["id"],
            api_key=row["api_key"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class UpstreamRuntimeManager:
    def __init__(self, db: Database, app_state: Any):
        self.repository = NovelAIUpstreamRepository(db)
        self._app_state = app_state

    def sync(self) -> dict[str, UpstreamClient]:
        enabled = self.repository.list(include_disabled=False)
        clients = {
            record.id: UpstreamClient(record.api_key)
            for record in enabled
        }
        self._replace_state_clients(clients)
        self._sync_queue_targets()
        return clients

    def reload_upstream(self, upstream_id: str) -> None:
        record = self.repository.get(upstream_id)
        clients = dict(getattr(self._app_state, "upstream_clients", {}) or {})
        if record is None or not record.enabled:
            clients.pop(upstream_id, None)
        else:
            clients[record.id] = UpstreamClient(record.api_key)
        self._replace_state_clients(clients)
        self._sync_queue_targets()

    def disable_upstream(self, upstream_id: str) -> NovelAIUpstreamRecord | None:
        record = self.repository.get(upstream_id)
        if record is None or not record.enabled:
            return None
        updated = self.repository.update(upstream_id, enabled=False)
        self.reload_upstream(upstream_id)
        return updated

    def list_upstream_ids(self, *, include_disabled: bool = True) -> list[str]:
        return [record.id for record in self.repository.list(include_disabled=include_disabled)]

    def get_settings(self) -> NovelAISettings:
        return self.repository.get_settings()

    def client_provider_for(self, upstream_id: str) -> Callable[[], Any]:
        def provider() -> Any:
            # Keep runtime app.state lookup compatibility for tests and admin hot-swaps.
            if upstream_id == self._app_state.default_upstream_id:
                return self._app_state.upstream
            return self._app_state.upstream_clients[upstream_id]

        return provider

    def queue_targets(self) -> list[UpstreamQueueTarget]:
        return [
            UpstreamQueueTarget(
                id=upstream_id,
                client_provider=self.client_provider_for(upstream_id),
            )
            for upstream_id in getattr(self._app_state, "upstream_clients", {})
        ]

    def _replace_state_clients(self, clients: dict[str, UpstreamClient]) -> None:
        self._app_state.upstream_clients = clients
        default_upstream_id = next(iter(clients), None)
        self._app_state.default_upstream_id = default_upstream_id
        self._app_state.upstream = clients[default_upstream_id] if default_upstream_id is not None else None

    def _sync_queue_targets(self) -> None:
        queue = getattr(self._app_state, "proxy_queue", None)
        sync_targets = getattr(queue, "sync_targets", None)
        if not callable(sync_targets):
            return
        sync_targets(self.queue_targets())
