from __future__ import annotations

import asyncio
import dataclasses
import heapq
import itertools
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

from novelai_python._exceptions import APIError

from .config import LoggingConfig
from .logging_utils import archive_zip_images, logger
from .queue_errors import (
    NoAvailableUpstream,
    QueueClosed,
    QueueFull,
    Retry429Error,
    UpstreamItemRerouted,
    UpstreamExecutionTimeout,
    UserUnavailable,
)
from .queue_models import AdaptiveUpstreamScore, ImageHostingServiceLike, QueueItem, UpstreamQueueTarget
from .queue_snapshot import ProxyQueueSnapshot, QueueItemSnapshot, QueueSnapshotStatus, RoutingQueueSnapshot
from .queue_snapshot_helpers import item_snapshot as _item_snapshot
from .queue_snapshot_helpers import without_sequence as _without_sequence
from .queue_tiers import (
    QUEUE_PRIORITY_NORMAL,
    QUEUE_PRIORITY_VIP,
    QUEUE_PRIORITY_VIP_RETRY,
    priority_for_tier,
    tier_allows_overflow,
)
from .queue_work_queue import PriorityWorkQueue
from .quota_manager import QuotaManager
from .request_accounting import RequestAccounting
from .retry_policy import RetryDecision, RetryPolicy
from .routing_queue import RoutingProxyQueue
from .upstream_queue import ProxyQueue
from .usage_logs import UsageLogRepository
