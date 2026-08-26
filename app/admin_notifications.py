from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .database import Database, utc_now_iso
from .logging_utils import json_dumps


@dataclass(frozen=True)
class AdminNotification:
    id: int
    event_type: str
    title: str
    content: str
    event_time: str
    metadata: dict[str, Any]
    created_at: str
    dismissed_at: str | None


class AdminNotificationRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        *,
        event_type: str,
        title: str,
        content: str,
        event_time: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AdminNotification:
        timestamp = utc_now_iso()
        cursor = self.db.execute(
            """
            INSERT INTO admin_notifications(event_type, title, content, event_time, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                title,
                content,
                event_time or timestamp,
                json_dumps(metadata or {}),
                timestamp,
            ),
        )
        row = self.db.query_one(
            """
            SELECT id, event_type, title, content, event_time, metadata, created_at, dismissed_at
            FROM admin_notifications
            WHERE id = ?
            """,
            (int(cursor.lastrowid),),
        )
        assert row is not None
        return self._row_to_notification(row)

    def pending(self) -> list[AdminNotification]:
        rows = self.db.query_all(
            """
            SELECT id, event_type, title, content, event_time, metadata, created_at, dismissed_at
            FROM admin_notifications
            WHERE dismissed_at IS NULL
            ORDER BY event_time ASC, id ASC
            """
        )
        return [self._row_to_notification(row) for row in rows]

    def pending_upstream_ids(self, event_type: str) -> set[str]:
        """只取某类未处理通知的 upstream_id，避免为无关通知构造对象。"""
        rows = self.db.query_all(
            """
            SELECT metadata
            FROM admin_notifications
            WHERE dismissed_at IS NULL AND event_type = ?
            """,
            (event_type,),
        )
        ids: set[str] = set()
        for row in rows:
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(metadata, dict):
                continue
            upstream_id = metadata.get("upstream_id")
            if upstream_id:
                ids.add(str(upstream_id))
        return ids

    def dismiss(self, notification_id: int) -> bool:
        cursor = self.db.execute(
            """
            UPDATE admin_notifications
            SET dismissed_at = COALESCE(dismissed_at, ?)
            WHERE id = ?
            """,
            (utc_now_iso(), int(notification_id)),
        )
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_notification(row) -> AdminNotification:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return AdminNotification(
            id=int(row["id"]),
            event_type=row["event_type"],
            title=row["title"],
            content=row["content"],
            event_time=row["event_time"],
            metadata=metadata,
            created_at=row["created_at"],
            dismissed_at=row["dismissed_at"],
        )
