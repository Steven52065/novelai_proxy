from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from sqlite3 import Row
from collections.abc import Callable
from typing import Any

from .config import HotPayloadConfig
from .database import Database, utc_now_iso
from .logging_utils import json_dumps


USAGE_LOG_STATUSES = ("queued", "running", "success", "failed", "rejected")


USAGE_LOG_SELECT_COLUMNS = """
    l.id, l.request_id, l.attempt_number, l.user_id, l.action, l.model, l.width, l.height,
    l.steps, l.n_samples, l.estimated_anlas_cost, l.final_anlas_cost, l.queued_ms,
    l.upstream_ms, l.total_ms, l.status, l.error_code, l.error_message, l.log_level,
    l.upstream_id,
    CASE
        WHEN r.log_id IS NULL AND l.request_payload_encoding = 'json' THEN l.request_payload
        ELSE NULL
    END AS request_payload,
    l.output_files, l.image_urls, l.is_retry_success, l.created_at, l.completed_at,
    CASE
        WHEN (l.request_payload IS NOT NULL AND l.request_payload != '')
          OR (l.request_payload_blob IS NOT NULL AND LENGTH(l.request_payload_blob) > 0)
          OR r.log_id IS NOT NULL THEN 1
        ELSE 0
    END AS has_request_payload,
    CASE WHEN r.log_id IS NOT NULL THEN 1 ELSE 0 END AS payload_archived,
    CASE
        WHEN r.log_id IS NOT NULL THEN r.payload_bytes
        WHEN l.request_payload_bytes > 0 THEN l.request_payload_bytes
        WHEN l.request_payload IS NOT NULL THEN LENGTH(CAST(l.request_payload AS BLOB))
        ELSE 0
    END AS request_payload_bytes
"""


@dataclass(frozen=True)
class UsageLogCreate:
    request_id: str
    user_id: int
    action: str
    estimated_anlas_cost: int
    request_payload: dict[str, Any] | None = None
    model: str | None = None
    width: int | None = None
    height: int | None = None
    steps: int | None = None
    n_samples: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    log_level: str = "INFO"
    upstream_id: str | None = None


@dataclass(frozen=True)
class EncodedPayload:
    request_payload: str | None
    encoding: str
    blob: bytes | None
    original_bytes: int
    compressed_bytes: int


class UsageLogRepository:
    """Centralizes usage_logs writes so status transitions stay consistent."""

    def __init__(
        self,
        db: Database,
        on_change: Callable[[], None] | None = None,
        hot_payload_config: HotPayloadConfig | None = None,
    ):
        self.db = db
        self._on_change = on_change
        self.hot_payload_config = hot_payload_config or HotPayloadConfig()

    def insert_queued(self, log: UsageLogCreate, attempt_number: int = 0) -> None:
        payload = self._encode_payload(log.request_payload)
        self.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, attempt_number, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, error_code, error_message, log_level, upstream_id,
                request_payload, request_payload_encoding, request_payload_blob, request_payload_bytes,
                request_payload_compressed_bytes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log.request_id,
                attempt_number,
                log.user_id,
                log.action,
                log.model,
                log.width,
                log.height,
                log.steps,
                log.n_samples,
                int(log.estimated_anlas_cost),
                log.error_code,
                log.error_message,
                log.log_level,
                log.upstream_id,
                payload.request_payload,
                payload.encoding,
                payload.blob,
                payload.original_bytes,
                payload.compressed_bytes,
                utc_now_iso(),
            ),
        )
        self._notify_change()

    def insert_rejected(self, log: UsageLogCreate, attempt_number: int = 0) -> None:
        payload = self._encode_payload(log.request_payload)
        self.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, attempt_number, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, error_code, error_message, log_level, upstream_id,
                request_payload, request_payload_encoding, request_payload_blob, request_payload_bytes,
                request_payload_compressed_bytes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'rejected', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log.request_id,
                attempt_number,
                log.user_id,
                log.action,
                log.model,
                log.width,
                log.height,
                log.steps,
                log.n_samples,
                int(log.estimated_anlas_cost),
                log.error_code,
                log.error_message,
                log.log_level,
                log.upstream_id,
                payload.request_payload,
                payload.encoding,
                payload.blob,
                payload.original_bytes,
                payload.compressed_bytes,
                utc_now_iso(),
            ),
        )
        self._notify_change()

    def mark_running(self, request_id: str, queued_ms: int, upstream_id: str | None = None, attempt_number: int = 0) -> None:
        self.db.execute(
            """
            UPDATE usage_logs
            SET status = 'running', queued_ms = ?, upstream_id = COALESCE(?, upstream_id)
            WHERE request_id = ? AND attempt_number = ?
            """,
            (queued_ms, upstream_id, request_id, attempt_number),
        )
        self._notify_change()

    def mark_success(
        self,
        request_id: str,
        *,
        queued_ms: int,
        final_cost: int,
        output_files: list[dict[str, object]],
        image_urls: list[dict[str, object]] | None = None,
        upstream_ms: int | None = None,
        is_retry_success: bool = False,
        attempt_number: int = 0,
    ) -> None:
        self.db.execute(
            """
            UPDATE usage_logs
            SET status = 'success',
                queued_ms = COALESCE(queued_ms, ?),
                upstream_ms = COALESCE(upstream_ms, ?),
                final_anlas_cost = ?,
                output_files = ?,
                image_urls = COALESCE(image_urls, ?),
                is_retry_success = ?,
                error_code = NULL,
                error_message = NULL,
                completed_at = ?
            WHERE request_id = ? AND attempt_number = ?
            """,
            (
                queued_ms,
                upstream_ms,
                final_cost,
                json_dumps(output_files),
                json_dumps([] if image_urls is None else image_urls),
                1 if is_retry_success else 0,
                utc_now_iso(),
                request_id,
                attempt_number,
            ),
        )
        self._notify_change()

    def mark_failed(
        self,
        request_id: str,
        *,
        queued_ms: int,
        error_code: str,
        error_message: str,
        upstream_ms: int | None = None,
        attempt_number: int = 0,
    ) -> None:
        self.db.execute(
            """
            UPDATE usage_logs
            SET status = 'failed',
                queued_ms = COALESCE(queued_ms, ?),
                upstream_ms = COALESCE(upstream_ms, ?),
                error_code = ?,
                error_message = ?,
                log_level = 'ERROR',
                completed_at = ?
            WHERE request_id = ? AND attempt_number = ?
            """,
            (queued_ms, upstream_ms, error_code, error_message[:500], utc_now_iso(), request_id, attempt_number),
        )
        self._notify_change()

    def mark_rejected(
        self,
        request_id: str,
        *,
        error_code: str,
        error_message: str,
        log_level: str = "ERROR",
        attempt_number: int = 0,
    ) -> None:
        self.db.execute(
            """
            UPDATE usage_logs
            SET status = 'rejected',
                error_code = ?,
                error_message = ?,
                log_level = ?,
                completed_at = ?
            WHERE request_id = ? AND attempt_number = ?
            """,
            (error_code, error_message, log_level, utc_now_iso(), request_id, attempt_number),
        )
        self._notify_change()

    def update_image_urls(self, request_id: str, image_urls: list[dict[str, object]], attempt_number: int = 0) -> None:
        self.db.execute(
            """
            UPDATE usage_logs
            SET image_urls = ?
            WHERE request_id = ? AND attempt_number = ?
            """,
            (json_dumps(image_urls), request_id, attempt_number),
        )
        self._notify_change()

    def mark_total_duration(self, request_id: str, total_ms: int, attempt_number: int | None = None) -> None:
        if attempt_number is None:
            self.db.execute(
                """
                UPDATE usage_logs
                SET total_ms = ?
                WHERE request_id = ?
                """,
                (total_ms, request_id),
            )
        else:
            self.db.execute(
                """
                UPDATE usage_logs
                SET total_ms = ?
                WHERE request_id = ? AND attempt_number = ?
                """,
                (total_ms, request_id, attempt_number),
            )
        self._notify_change()

    def insert_retry_attempt(
        self,
        *,
        request_id: str | None = None,
        attempt_number: int,
        upstream_id: str | None = None,
    ) -> None:
        """为重试尝试复制初始日志行并插入新的 running 记录。"""
        if request_id is None:
            raise ValueError("Must provide request_id")
        self.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, attempt_number, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, log_level, upstream_id, request_payload,
                request_payload_encoding, request_payload_blob, request_payload_bytes,
                request_payload_compressed_bytes, created_at
            )
            SELECT
                request_id, ?, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, 'running', 'INFO', ?, request_payload,
                request_payload_encoding, request_payload_blob, request_payload_bytes,
                request_payload_compressed_bytes, ?
            FROM usage_logs
            WHERE request_id = ? AND attempt_number = 0
            """,
            (attempt_number, upstream_id, utc_now_iso(), request_id),
        )
        self._notify_change()

    def get_by_request_id(self, request_id: str) -> Row | None:
        return self.db.query_one(
            f"""
            SELECT {USAGE_LOG_SELECT_COLUMNS}
            FROM usage_logs l
            LEFT JOIN usage_log_payload_archive_refs r ON r.log_id = l.id
            WHERE l.request_id = ?
            ORDER BY attempt_number DESC, id DESC
            """,
            (request_id,),
        )

    def get_by_id(self, log_id: int) -> Row | None:
        return self.db.query_one(
            f"""
            SELECT {USAGE_LOG_SELECT_COLUMNS}
            FROM usage_logs l
            LEFT JOIN usage_log_payload_archive_refs r ON r.log_id = l.id
            WHERE l.id = ?
            """,
            (log_id,),
        )

    def list_actions(self) -> list[str]:
        rows = self.db.query_all(
            """
            SELECT DISTINCT action
            FROM usage_logs
            ORDER BY action
            """
        )
        return [str(row["action"]) for row in rows]

    def list_logs(
        self,
        *,
        user_id: int | None,
        limit: int,
        before_id: int | None,
        created_from: str | None = None,
        created_to: str | None = None,
        action: str | None = None,
        status: str | None = None,
    ) -> list[Row]:
        fetch_limit = limit + 1
        where: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            where.append("l.user_id = ?")
            params.append(user_id)
        if action is not None:
            where.append("l.action = ?")
            params.append(action)
        if status is not None:
            where.append("l.status = ?")
            params.append(status)
        if created_from is not None:
            where.append("l.created_at >= ?")
            params.append(created_from)
        if created_to is not None:
            where.append("l.created_at < ?")
            params.append(created_to)
        if before_id is not None:
            where.append("l.id < ?")
            params.append(before_id)

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(fetch_limit)
        return self.db.query_all(
            f"""
            SELECT {USAGE_LOG_SELECT_COLUMNS}, u.name AS user_name
            FROM usage_logs l
            JOIN users u ON u.id = l.user_id
            LEFT JOIN usage_log_payload_archive_refs r ON r.log_id = l.id
            {where_sql}
            ORDER BY l.id DESC
            LIMIT ?
            """,
            tuple(params),
        )

    def _encode_payload(self, payload: dict[str, Any] | None) -> EncodedPayload:
        if payload is None:
            return EncodedPayload(
                request_payload=None,
                encoding="json",
                blob=None,
                original_bytes=0,
                compressed_bytes=0,
            )

        payload_json = self._payload_json(payload)
        raw = payload_json.encode("utf-8")
        config = self.hot_payload_config
        if (
            config.enabled
            and config.compression == "zlib"
            and len(raw) >= int(config.min_bytes)
        ):
            compressed = zlib.compress(raw, int(config.compression_level))
            saved = len(raw) - len(compressed)
            if saved >= len(raw) * float(config.min_savings_ratio):
                return EncodedPayload(
                    request_payload=None,
                    encoding="zlib",
                    blob=compressed,
                    original_bytes=len(raw),
                    compressed_bytes=len(compressed),
                )

        return EncodedPayload(
            request_payload=payload_json,
            encoding="json",
            blob=None,
            original_bytes=len(raw),
            compressed_bytes=0,
        )

    @staticmethod
    def _payload_json(payload: dict[str, Any]) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def _notify_change(self) -> None:
        if self._on_change is not None:
            self._on_change()
