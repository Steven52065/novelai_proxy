from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Row
from typing import Any

from .database import Database, utc_now_iso
from .logging_utils import json_dumps


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


class UsageLogRepository:
    """Centralizes usage_logs writes so status transitions stay consistent."""

    def __init__(self, db: Database):
        self.db = db

    def insert_queued(self, log: UsageLogCreate, attempt_number: int = 0) -> None:
        self.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, attempt_number, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, error_code, error_message, log_level, upstream_id, request_payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
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
                self._payload_json(log.request_payload),
                utc_now_iso(),
            ),
        )

    def insert_rejected(self, log: UsageLogCreate, attempt_number: int = 0) -> None:
        self.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, attempt_number, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, error_code, error_message, log_level, upstream_id, request_payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'rejected', ?, ?, ?, ?, ?, ?)
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
                self._payload_json(log.request_payload),
                utc_now_iso(),
            ),
        )

    def mark_running(self, request_id: str, queued_ms: int, upstream_id: str | None = None, attempt_number: int = 0) -> None:
        self.db.execute(
            """
            UPDATE usage_logs
            SET status = 'running', queued_ms = ?, upstream_id = COALESCE(?, upstream_id)
            WHERE request_id = ? AND attempt_number = ?
            """,
            (queued_ms, upstream_id, request_id, attempt_number),
        )

    def mark_success(
        self,
        request_id: str,
        *,
        queued_ms: int,
        final_cost: int,
        output_files: list[dict[str, object]],
        image_urls: list[dict[str, object]] | None = None,
        is_retry_success: bool = False,
        attempt_number: int = 0,
    ) -> None:
        self.db.execute(
            """
            UPDATE usage_logs
            SET status = 'success',
                queued_ms = COALESCE(queued_ms, ?),
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
                final_cost,
                json_dumps(output_files),
                json_dumps([] if image_urls is None else image_urls),
                1 if is_retry_success else 0,
                utc_now_iso(),
                request_id,
                attempt_number,
            ),
        )

    def mark_failed(self, request_id: str, *, queued_ms: int, error_code: str, error_message: str, attempt_number: int = 0) -> None:
        self.db.execute(
            """
            UPDATE usage_logs
            SET status = 'failed',
                queued_ms = COALESCE(queued_ms, ?),
                error_code = ?,
                error_message = ?,
                log_level = 'ERROR',
                completed_at = ?
            WHERE request_id = ? AND attempt_number = ?
            """,
            (queued_ms, error_code, error_message[:500], utc_now_iso(), request_id, attempt_number),
        )

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

    def update_image_urls(self, request_id: str, image_urls: list[dict[str, object]], attempt_number: int = 0) -> None:
        self.db.execute(
            """
            UPDATE usage_logs
            SET image_urls = ?
            WHERE request_id = ? AND attempt_number = ?
            """,
            (json_dumps(image_urls), request_id, attempt_number),
        )

    def insert_retry_attempt(
        self,
        log: UsageLogCreate,
        *,
        attempt_number: int,
        upstream_id: str | None = None,
    ) -> None:
        """为重试尝试插入新的数据库记录（状态为 running）"""
        self.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, attempt_number, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, log_level, upstream_id, request_payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 'INFO', ?, ?, ?)
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
                upstream_id,
                self._payload_json(log.request_payload),
                utc_now_iso(),
            ),
        )

    def get_by_request_id(self, request_id: str) -> Row | None:
        return self.db.query_one(
            """
            SELECT *
            FROM usage_logs
            WHERE request_id = ?
            """,
            (request_id,),
        )

    def list_logs(self, *, user_id: int | None, limit: int, before_id: int | None) -> list[Row]:
        fetch_limit = limit + 1
        if user_id is None and before_id is None:
            return self.db.query_all(
                """
                SELECT l.*, u.name AS user_name
                FROM usage_logs l
                JOIN users u ON u.id = l.user_id
                ORDER BY l.id DESC
                LIMIT ?
                """,
                (fetch_limit,),
            )
        if user_id is None:
            return self.db.query_all(
                """
                SELECT l.*, u.name AS user_name
                FROM usage_logs l
                JOIN users u ON u.id = l.user_id
                WHERE l.id < ?
                ORDER BY l.id DESC
                LIMIT ?
                """,
                (before_id, fetch_limit),
            )
        if before_id is None:
            return self.db.query_all(
                """
                SELECT l.*, u.name AS user_name
                FROM usage_logs l
                JOIN users u ON u.id = l.user_id
                WHERE l.user_id = ?
                ORDER BY l.id DESC
                LIMIT ?
                """,
                (user_id, fetch_limit),
            )
        return self.db.query_all(
            """
            SELECT l.*, u.name AS user_name
            FROM usage_logs l
            JOIN users u ON u.id = l.user_id
            WHERE l.user_id = ? AND l.id < ?
            ORDER BY l.id DESC
            LIMIT ?
            """,
            (user_id, before_id, fetch_limit),
        )

    @staticmethod
    def _payload_json(payload: dict[str, Any] | None) -> str | None:
        return None if payload is None else json_dumps(payload)
