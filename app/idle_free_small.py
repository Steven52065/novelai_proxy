from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .free_small_daily_limit import FreeSmallDailyLimitSnapshot


@dataclass(frozen=True)
class IdleFreeSmallContext:
    """保存正常每日通道拒绝时的快照，用于在排队后复用原错误响应。"""

    daily_snapshot: FreeSmallDailyLimitSnapshot
    requested: int


@dataclass(frozen=True)
class PoolState:
    at: float
    enabled_ids: frozenset[str]
    busy_ids: frozenset[str]


class IdleFreeSmallTracker:
    """记录各上游串行 worker 的占用历史，供空闲免费小图兜底做准入判断。"""

    def __init__(
        self,
        *,
        occupancy_threshold_percent: float = 50,
        min_idle_seconds: float = 30,
        clock: Callable[[], float] | None = None,
    ):
        self.lock = threading.RLock()
        self.clock = clock or time.monotonic
        self.min_idle_seconds = _finite_nonnegative(min_idle_seconds)
        threshold_decimal = _finite_percent(occupancy_threshold_percent)
        self._threshold_num, self._threshold_den = threshold_decimal.as_integer_ratio()
        self.started_at: float | None = None
        self.active_workers: dict[str, object] = {}
        self.running_sources: dict[object, str] = {}
        self.events: deque[PoolState] = deque()

    def start(self) -> None:
        with self.lock:
            if self.started_at is None:
                self.started_at = self.clock()
                self._record_state_locked()

    def attach(self, upstream_id: str, worker_token: object) -> None:
        with self.lock:
            self.active_workers[upstream_id] = worker_token
            if self.started_at is not None:
                self._record_state_locked()

    def detach(self, upstream_id: str, worker_token: object) -> None:
        with self.lock:
            if self.active_workers.get(upstream_id) is not worker_token:
                return
            del self.active_workers[upstream_id]
            self.running_sources.pop(worker_token, None)
            if self.started_at is not None:
                self._record_state_locked()

    def record_started(self, worker_token: object, upstream_id: str) -> None:
        with self.lock:
            if self.active_workers.get(upstream_id) is not worker_token:
                return
            self.running_sources[worker_token] = upstream_id
            if self.started_at is not None:
                self._record_state_locked()

    def record_finished(self, worker_token: object) -> None:
        with self.lock:
            if worker_token not in self.running_sources:
                return
            del self.running_sources[worker_token]
            if self.started_at is not None:
                self._record_state_locked()

    def is_idle(self, allowed_upstreams: Iterable[str] | None = None, *, now: float | None = None) -> bool:
        with self.lock:
            return self._is_idle_locked(allowed_upstreams, now if now is not None else self.clock())

    def _is_idle_locked(self, allowed_upstreams: Iterable[str] | None, now_value: float) -> bool:
        if self.started_at is None:
            return False
        self._prune_locked(now_value)
        allowed = frozenset(allowed_upstreams or ())
        below_since: float | None = None
        for event in self.events:
            scope = event.enabled_ids & allowed if allowed else event.enabled_ids
            total = len(scope)
            busy = len(event.busy_ids & scope)
            if total > 0 and self._strictly_below_locked(busy, total):
                if below_since is None:
                    below_since = event.at
            else:
                below_since = None
        return below_since is not None and now_value - below_since >= self.min_idle_seconds

    def _strictly_below_locked(self, busy: int, total: int) -> bool:
        return 100 * busy * self._threshold_den < total * self._threshold_num

    def _prune_locked(self, now_value: float) -> None:
        cutoff = now_value - self.min_idle_seconds
        while len(self.events) > 1 and self.events[1].at <= cutoff:
            self.events.popleft()

    def _record_state_locked(self) -> None:
        now_value = self.clock()
        enabled_ids = frozenset(self.active_workers)
        busy_ids = frozenset(self.running_sources.values()) & enabled_ids
        if not self.events or self.events[-1].enabled_ids != enabled_ids or self.events[-1].busy_ids != busy_ids:
            self.events.append(PoolState(at=now_value, enabled_ids=enabled_ids, busy_ids=busy_ids))
        # 普通流量也会记录占用，不能等到有人申请空闲兜底才回收历史。
        self._prune_locked(now_value)


def _finite_nonnegative(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be a finite nonnegative number") from exc
    if number < 0 or number != number or number in (float("inf"), float("-inf")):
        raise ValueError("value must be a finite nonnegative number")
    return number


def _finite_percent(value: object) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("percent must be a finite number between 0 and 100") from exc
    if not number.is_finite() or number < 0 or number > 100:
        raise ValueError("percent must be a finite number between 0 and 100")
    return number
