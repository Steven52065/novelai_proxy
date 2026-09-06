from __future__ import annotations

from dataclasses import dataclass

from .free_small_daily_limit import FreeSmallDailyLimitSnapshot


@dataclass(frozen=True)
class IdleFreeSmallContext:
    """保存正常每日通道拒绝时的快照，用于在排队后复用原错误响应。"""

    daily_snapshot: FreeSmallDailyLimitSnapshot
    requested: int
