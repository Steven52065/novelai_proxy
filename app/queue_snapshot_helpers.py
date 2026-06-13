from __future__ import annotations

from .queue_models import QueueItem
from .queue_snapshot import QueueItemSnapshot, QueueSnapshotStatus


def item_snapshot(item: QueueItem, now: float, status: QueueSnapshotStatus, position: int) -> QueueItemSnapshot:
    return {
        "request_id": item.request_id,
        "user_id": item.user_id,
        "action": item.action,
        "tier": item.tier,
        "upstream_id": item.upstream_id,
        "estimated_anlas_cost": item.estimated_cost,
        "priority": item.priority,
        "sequence": item.sequence,
        "position": position,
        "status": status,
        "queued_seconds": max(0, int(now - item.enqueued_at)),
    }


def without_sequence(item: QueueItemSnapshot) -> QueueItemSnapshot:
    clean = dict(item)
    clean.pop("sequence", None)
    return clean
