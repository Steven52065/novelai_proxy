from __future__ import annotations

from dataclasses import dataclass

# 与 RoutingProxyQueue 的优先级常量保持一致的默认值，仅用于直接构造
# ProxyQueue（如单元测试）时的兜底策略；这些字段在执行层并不会被读取。
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_VIP_RETRY_PRIORITY = -1


@dataclass(frozen=True)
class RetryDecision:
    """一次 429 重试的决策结果。

    - ``should_retry`` 为 ``False`` 表示已达尝试上限，应放弃并按上游不可用处理。
    - ``should_retry`` 为 ``True`` 时，``next_attempt_number`` 是下一次尝试的序号，
      ``priority`` 是重排时使用的优先级，``immediate`` 区分两种重排方式：
      ``True`` 表示 VIP 同步旁路（立即重新派发到上游），
      ``False`` 表示普通用户重新进入调度队列。
    """

    should_retry: bool
    next_attempt_number: int
    priority: int
    immediate: bool


class RetryPolicy:
    """集中承载 429 重试的全部判定逻辑。

    把原先散布在执行层（``ProxyQueue``，是否值得抛出 ``Retry429Error``）和
    调度层（``RoutingProxyQueue``，是否放弃、如何重排）的判定收敛到一个纯逻辑
    对象里，调度层只负责执行决策。判定本身不带任何 IO 或 asyncio 状态，可被
    决策表式地单测。
    """

    def __init__(
        self,
        *,
        queue_length_threshold: int,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        vip_retry_priority: int = DEFAULT_VIP_RETRY_PRIORITY,
    ) -> None:
        self.queue_length_threshold = int(queue_length_threshold)
        self.max_attempts = int(max_attempts)
        self.vip_retry_priority = int(vip_retry_priority)

    def should_attempt_429_retry(self, total_queue_length: int) -> bool:
        """执行层判定：当前所有上游的总排队长度是否允许把 429 转为重试。

        阈值为负数表示完全关闭重试；否则仅当总排队长度不超过阈值时才重试，
        避免在已经很拥塞时继续放大队列。
        """
        if self.queue_length_threshold < 0:
            return False
        return total_queue_length <= self.queue_length_threshold

    def decide_retry(
        self,
        *,
        tier: str,
        attempt_number: int,
        current_priority: int,
    ) -> RetryDecision:
        """调度层判定：收到 ``Retry429Error`` 后是放弃还是如何重排。

        ``attempt_number`` 是刚失败这次尝试的序号；下一次尝试序号达到上限即放弃。
        VIP 重试使用专门的更高优先级并走同步旁路立即重派；普通用户沿用当前优先级
        重新进入调度队列。
        """
        next_attempt_number = attempt_number + 1
        if next_attempt_number >= self.max_attempts:
            return RetryDecision(
                should_retry=False,
                next_attempt_number=next_attempt_number,
                priority=current_priority,
                immediate=False,
            )
        is_vip = tier == "vip"
        return RetryDecision(
            should_retry=True,
            next_attempt_number=next_attempt_number,
            priority=self.vip_retry_priority if is_vip else current_priority,
            immediate=is_vip,
        )
