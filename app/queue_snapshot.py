from __future__ import annotations

from typing import Literal, TypedDict


QueueSnapshotStatus = Literal["queued", "running", "dispatch_queued"]


class QueueItemSnapshot(TypedDict, total=False):
    request_id: str
    user_id: int
    user_name: str | None
    action: str
    tier: str
    upstream_id: str | None
    model: str | None
    width: int | None
    height: int | None
    steps: int | None
    n_samples: int | None
    estimated_anlas_cost: int
    priority: int
    sequence: int
    position: int
    status: QueueSnapshotStatus
    queued_seconds: int
    running_seconds: int


class ProxyQueueSnapshot(TypedDict):
    queue_size: int
    running: QueueItemSnapshot | None
    queued: list[QueueItemSnapshot]


class UpstreamQueueSnapshot(ProxyQueueSnapshot):
    id: str


class RoutingQueueSnapshot(TypedDict):
    queue_size: int
    running: QueueItemSnapshot | None
    running_items: list[QueueItemSnapshot]
    queued: list[QueueItemSnapshot]
    dispatch_queue_size: int
    upstreams: list[UpstreamQueueSnapshot]
