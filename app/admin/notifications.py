from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..admin_notifications import AdminNotification
from .auth import require_admin_or_session
from .common import format_display_time


api_router = APIRouter(prefix="/admin/api")


@api_router.get("/notifications/pending", dependencies=[Depends(require_admin_or_session)])
async def pending_notifications(request: Request):
    repo = request.app.state.admin_notifications
    return {
        "notifications": [
            _notification_to_dict(notification)
            for notification in repo.pending()
        ]
    }


@api_router.post("/notifications/{notification_id}/dismiss", dependencies=[Depends(require_admin_or_session)])
async def dismiss_notification(request: Request, notification_id: int):
    repo = request.app.state.admin_notifications
    if not repo.dismiss(notification_id):
        raise HTTPException(status_code=404, detail={"message": "Notification not found"})
    return {"ok": True}


def _notification_to_dict(notification: AdminNotification) -> dict:
    return {
        "id": notification.id,
        "event_type": notification.event_type,
        "title": notification.title,
        "content": notification.content,
        "event_time": notification.event_time,
        "event_time_display": format_display_time(notification.event_time),
        "metadata": notification.metadata,
        "created_at": notification.created_at,
        "dismissed_at": notification.dismissed_at,
    }
