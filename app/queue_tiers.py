from __future__ import annotations

from dataclasses import dataclass


TIER_NORMAL = "normal"
TIER_VIP = "vip"
TIER_REPLAY = "replay"
USER_TIER_PATTERN = f"^({TIER_NORMAL}|{TIER_VIP})$"

QUEUE_PRIORITY_REPLAY = -100
QUEUE_PRIORITY_VIP_RETRY = -1
QUEUE_PRIORITY_VIP = 0
QUEUE_PRIORITY_NORMAL = 10


@dataclass(frozen=True)
class QueueTierPolicy:
    priority: int
    allow_overflow: bool


QUEUE_TIER_POLICIES = {
    TIER_NORMAL: QueueTierPolicy(priority=QUEUE_PRIORITY_NORMAL, allow_overflow=False),
    TIER_VIP: QueueTierPolicy(priority=QUEUE_PRIORITY_VIP, allow_overflow=True),
    TIER_REPLAY: QueueTierPolicy(priority=QUEUE_PRIORITY_REPLAY, allow_overflow=False),
}


def queue_policy_for_tier(tier: str) -> QueueTierPolicy:
    return QUEUE_TIER_POLICIES.get(tier, QUEUE_TIER_POLICIES[TIER_NORMAL])


def priority_for_tier(tier: str) -> int:
    return queue_policy_for_tier(tier).priority


def tier_allows_overflow(tier: str) -> bool:
    return queue_policy_for_tier(tier).allow_overflow


def is_vip_tier(tier: str) -> bool:
    return tier == TIER_VIP
