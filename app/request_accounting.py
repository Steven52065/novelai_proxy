from __future__ import annotations

from typing import Any

from .free_small_daily_limit import FreeSmallDailyLimitManager, FreeSmallDailyReservation
from .logging_utils import logger


class RequestAccounting:
    """单个代理请求的记账生命周期对象。

    持有一次请求的三类记账状态：

    - anlas 额度预留：``quota_manager`` + ``user_id`` + ``estimated_cost``，
      ``manage_quota=False`` 表示本请求不做额度记账（例如管理员重放）。
    - 每日小图预约：``free_small_daily_limit_manager`` + ``free_small_daily_reservation``。
    - usage_logs 状态流转：``usage_logs`` 仓库 + ``request_id``。

    核心不变量："reserved 额度必须被 release 或 confirm 恰好一次"。
    ``settle_success`` / ``settle_failure`` / ``settle_rejected`` / ``settle_released``
    中只有第一次调用会真正结算预留，之后的重复调用对预留是安全的 no-op。

    日志终态写入按"每条 attempt 日志行至多写一次终态"独立守护：
    队列层先 ``settle_released``（只释放预留）、service 层随后补写 rejected
    日志（队列满 / 无可用上游路径）时，终态日志仍会写入，但预留不会被二次释放。

    429 重试场景：``record_retry_failure`` 把当前 attempt 的日志行标记为
    failed 但保留预留；``record_retry_attempt`` 为新一次尝试插入新的日志行，
    插入失败时立即释放预留并重新抛出异常。
    """

    def __init__(
        self,
        *,
        quota_manager: Any,
        usage_logs: Any,
        request_id: str,
        user_id: int,
        estimated_cost: int,
        manage_quota: bool = True,
        free_small_daily_limit_manager: FreeSmallDailyLimitManager | None = None,
        free_small_daily_reservation: FreeSmallDailyReservation | None = None,
    ):
        self.quota_manager = quota_manager
        self.usage_logs = usage_logs
        self.request_id = request_id
        self.user_id = user_id
        self.estimated_cost = estimated_cost
        self.manage_quota = manage_quota
        self.free_small_daily_limit_manager = free_small_daily_limit_manager
        self.free_small_daily_reservation = free_small_daily_reservation
        self._reservation_settled = False
        self._log_finalized = False

    @property
    def settled(self) -> bool:
        return self._reservation_settled

    def settle_success(
        self,
        *,
        queued_ms: int,
        final_cost: int,
        output_files: list[dict[str, object]],
        upstream_ms: int | None = None,
        is_retry_success: bool = False,
        attempt_number: int = 0,
    ) -> None:
        """成功结算：确认额度与每日预约，并把日志行标记为 success。"""
        if self._try_settle_reservation():
            if self.manage_quota:
                self.quota_manager.confirm(self.user_id, self.estimated_cost)
            self._confirm_free_small_daily_reservation()
        if self._try_finalize_log():
            self.usage_logs.mark_success(
                self.request_id,
                queued_ms=queued_ms,
                final_cost=final_cost,
                output_files=output_files,
                upstream_ms=upstream_ms,
                is_retry_success=is_retry_success,
                attempt_number=attempt_number,
            )

    def settle_failure(
        self,
        *,
        queued_ms: int,
        error_code: str,
        error_message: str,
        upstream_ms: int | None = None,
        attempt_number: int = 0,
    ) -> None:
        """失败结算：释放额度与每日预约，并把日志行标记为 failed。"""
        self.settle_released()
        if self._try_finalize_log():
            self.usage_logs.mark_failed(
                self.request_id,
                queued_ms=queued_ms,
                error_code=error_code,
                error_message=error_message,
                upstream_ms=upstream_ms,
                attempt_number=attempt_number,
            )

    def settle_rejected(
        self,
        *,
        error_code: str,
        error_message: str,
        log_level: str = "ERROR",
        attempt_number: int = 0,
    ) -> None:
        """拒绝结算：释放额度与每日预约，并把日志行标记为 rejected。"""
        self.settle_released()
        if self._try_finalize_log():
            self.usage_logs.mark_rejected(
                self.request_id,
                error_code=error_code,
                error_message=error_message,
                log_level=log_level,
                attempt_number=attempt_number,
            )

    def settle_released(self) -> None:
        """只释放预留，不写日志终态。

        用于日志终态已经（或将要）在别处写入的路径：429 重试被取消、
        重试超过最大次数、重试重新入队失败、入队日志插入失败等。
        """
        if self._try_settle_reservation():
            if self.manage_quota:
                self.quota_manager.release(self.user_id, self.estimated_cost)
            self._release_free_small_daily_reservation()

    def record_retry_failure(
        self,
        *,
        queued_ms: int,
        error_code: str,
        error_message: str,
        upstream_ms: int | None = None,
        attempt_number: int = 0,
    ) -> None:
        """429 触发重试：把当前 attempt 的日志行标记为 failed，但保留预留。

        请求仍在重试中，额度保持 reserved 状态；若最终放弃重试，
        由后续的 settle_released / settle_failure 统一释放。
        """
        if self._try_finalize_log():
            self.usage_logs.mark_failed(
                self.request_id,
                queued_ms=queued_ms,
                error_code=error_code,
                error_message=error_message,
                upstream_ms=upstream_ms,
                attempt_number=attempt_number,
            )

    def record_retry_attempt(self, *, attempt_number: int, upstream_id: str | None) -> None:
        """为新一次重试尝试插入新的日志行（状态为 running）。

        插入失败时立即释放预留并重新抛出异常，避免预留额度悬挂
        （对应提交 7e3bae5 修复的问题）。
        """
        try:
            self.usage_logs.insert_retry_attempt(
                request_id=self.request_id,
                attempt_number=attempt_number,
                upstream_id=upstream_id,
            )
        except Exception:
            self.settle_released()
            raise
        self._log_finalized = False

    def _try_settle_reservation(self) -> bool:
        if self._reservation_settled:
            return False
        self._reservation_settled = True
        return True

    def _try_finalize_log(self) -> bool:
        if self._log_finalized:
            return False
        self._log_finalized = True
        return True

    def _release_free_small_daily_reservation(self) -> None:
        manager = self.free_small_daily_limit_manager
        reservation = self.free_small_daily_reservation
        if manager is None or reservation is None:
            return
        try:
            manager.release(reservation)
        except Exception:
            logger.exception("failed to release free small daily reservation user_id=%s", reservation.user_id)

    def _confirm_free_small_daily_reservation(self) -> None:
        manager = self.free_small_daily_limit_manager
        reservation = self.free_small_daily_reservation
        if manager is None or reservation is None:
            return
        try:
            manager.confirm(reservation)
        except Exception:
            logger.exception("failed to confirm free small daily reservation user_id=%s", reservation.user_id)


class NoopRequestAccounting:
    """Accounting adapter for admin probes that must not touch quota or logs."""

    manage_quota = False

    @property
    def settled(self) -> bool:
        return True

    def settle_success(self, **_kwargs) -> None:
        pass

    def settle_failure(self, **_kwargs) -> None:
        pass

    def settle_rejected(self, **_kwargs) -> None:
        pass

    def settle_released(self) -> None:
        pass

    def record_retry_failure(self, **_kwargs) -> None:
        pass

    def record_retry_attempt(self, **_kwargs) -> None:
        pass
