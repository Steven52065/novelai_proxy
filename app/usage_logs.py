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


class UsageLogRepository:
    """Centralizes usage_logs writes so status transitions stay consistent."""

    def __init__(self, db: Database):
        self.db = db

    def insert_queued(self, log: UsageLogCreate) -> None:
        self.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, error_code, error_message, log_level, request_payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (
                log.request_id,
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
                self._payload_json(log.request_payload),
                utc_now_iso(),
            ),
        )

    def insert_rejected(self, log: UsageLogCreate) -> None:
        self.db.execute(
            """
            INSERT INTO usage_logs (
                request_id, user_id, action, model, width, height, steps, n_samples,
                estimated_anlas_cost, status, error_code, error_message, log_level, request_payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'rejected', ?, ?, ?, ?, ?)
            """,
            (
                log.request_id,
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
                self._payload_json(log.request_payload),
                utc_now_iso(),
            ),
        )

    def mark_running(self, request_id: str, queued_ms: int) -> None:
        self.db.execute(
            """
            UPDATE usage_logs
            SET status = 'running', queued_ms = ?
            WHERE request_id = ?
            """,
            (queued_ms, request_id),
        )

    def mark_success(
        self,
        request_id: str,
        *,
        queued_ms: int,
        final_cost: int,
        output_files: list[dict[str, object]],
        image_urls: list[dict[str, object]] | None = None,
    ) -> None:
        self.db.execute(
            """
            UPDATE usage_logs
            SET status = 'success',
                queued_ms = COALESCE(queued_ms, ?),
                final_anlas_cost = ?,
                output_files = ?,
                image_urls = COALESCE(image_urls, ?),
                completed_at = ?
            WHERE request_id = ?
            """,
            (
                queued_ms,
                final_cost,
                json_dumps(output_files),
                json_dumps([] if image_urls is None else image_urls),
                utc_now_iso(),
                request_id,
            ),
        )

    def mark_failed(self, request_id: str, *, queued_ms: int, error_code: str, error_message: str) -> None:
        self.db.execute(
            """
            UPDATE usage_logs
            SET status = 'failed',
                queued_ms = COALESCE(queued_ms, ?),
                error_code = ?,
                error_message = ?,
                log_level = 'ERROR',
                completed_at = ?
            WHERE request_id = ?
            """,
            (queued_ms, error_code, error_message[:500], utc_now_iso(), request_id),
        )

    def mark_rejected(
        self,
        request_id: str,
        *,
        error_code: str,
        error_message: str,
        log_level: str = "ERROR",
    ) -> None:
        self.db.execute(
            """
            UPDATE usage_logs
            SET status = 'rejected',
                error_code = ?,
                error_message = ?,
                log_level = ?,
                completed_at = ?
            WHERE request_id = ?
            """,
            (error_code, error_message, log_level, utc_now_iso(), request_id),
        )

    def update_image_urls(self, request_id: str, image_urls: list[dict[str, object]]) -> None:
        self.db.execute(
            """
            UPDATE usage_logs
            SET image_urls = ?
            WHERE request_id = ?
            """,
            (json_dumps(image_urls), request_id),
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
