from __future__ import annotations

import pytest

from app.free_small_daily_limit import FreeSmallDailyReservation
from app.request_accounting import RequestAccounting


class RecordingQuota:
    def __init__(self, calls: list | None = None):
        self.calls = calls if calls is not None else []

    def confirm(self, user_id: int, cost: int) -> None:
        self.calls.append(("quota.confirm", user_id, cost))

    def release(self, user_id: int, cost: int) -> None:
        self.calls.append(("quota.release", user_id, cost))


class RecordingDaily:
    def __init__(self, calls: list | None = None):
        self.calls = calls if calls is not None else []

    def confirm(self, reservation) -> None:
        self.calls.append(("daily.confirm", reservation))

    def release(self, reservation) -> None:
        self.calls.append(("daily.release", reservation))


class FailingDaily:
    def confirm(self, reservation) -> None:
        raise RuntimeError("daily confirm failed")

    def release(self, reservation) -> None:
        raise RuntimeError("daily release failed")


class RecordingUsageLogs:
    def __init__(self, calls: list | None = None):
        self.calls = calls if calls is not None else []

    def mark_success(self, request_id, **kwargs) -> None:
        self.calls.append(("log.mark_success", request_id, kwargs))

    def mark_failed(self, request_id, **kwargs) -> None:
        self.calls.append(("log.mark_failed", request_id, kwargs))

    def mark_rejected(self, request_id, **kwargs) -> None:
        self.calls.append(("log.mark_rejected", request_id, kwargs))

    def insert_retry_attempt(self, *, request_id, attempt_number, upstream_id) -> None:
        self.calls.append(("log.insert_retry_attempt", request_id, attempt_number, upstream_id))


class FailingRetryInsertUsageLogs(RecordingUsageLogs):
    def insert_retry_attempt(self, **kwargs) -> None:
        raise RuntimeError("retry log insert failed")


def _reservation() -> FreeSmallDailyReservation:
    return FreeSmallDailyReservation(
        user_id=42,
        window_start="2026-06-08T00:00:00+08:00",
        count=1,
        scope="user",
        limit=2,
        reset_at="2026-06-09T00:00:00+08:00",
    )


def _accounting(*, calls: list, manage_quota: bool = True, with_daily: bool = True) -> RequestAccounting:
    daily = RecordingDaily(calls) if with_daily else None
    return RequestAccounting(
        quota_manager=RecordingQuota(calls),
        usage_logs=RecordingUsageLogs(calls),
        request_id="req-1",
        user_id=42,
        estimated_cost=7,
        manage_quota=manage_quota,
        free_small_daily_limit_manager=daily,
        free_small_daily_reservation=_reservation() if with_daily else None,
    )


def test_settle_success_confirms_quota_and_daily_then_marks_success():
    calls: list = []
    accounting = _accounting(calls=calls)

    accounting.settle_success(
        queued_ms=100,
        final_cost=7,
        output_files=[],
        upstream_ms=200,
        is_retry_success=False,
        attempt_number=0,
    )

    assert [call[0] for call in calls] == ["quota.confirm", "daily.confirm", "log.mark_success"]
    assert calls[0] == ("quota.confirm", 42, 7)
    assert calls[2][1] == "req-1"
    assert calls[2][2] == {
        "queued_ms": 100,
        "final_cost": 7,
        "output_files": [],
        "upstream_ms": 200,
        "is_retry_success": False,
        "attempt_number": 0,
    }
    assert accounting.settled


def test_settle_failure_releases_quota_and_daily_then_marks_failed():
    calls: list = []
    accounting = _accounting(calls=calls)

    accounting.settle_failure(
        queued_ms=100,
        error_code="upstream_error",
        error_message="boom",
        upstream_ms=50,
        attempt_number=1,
    )

    assert [call[0] for call in calls] == ["quota.release", "daily.release", "log.mark_failed"]
    assert calls[0] == ("quota.release", 42, 7)
    assert calls[2][2] == {
        "queued_ms": 100,
        "error_code": "upstream_error",
        "error_message": "boom",
        "upstream_ms": 50,
        "attempt_number": 1,
    }


def test_settle_rejected_releases_quota_and_daily_then_marks_rejected():
    calls: list = []
    accounting = _accounting(calls=calls)

    accounting.settle_rejected(
        error_code="user_unavailable",
        error_message="User is no longer active",
        log_level="INFO",
        attempt_number=0,
    )

    assert [call[0] for call in calls] == ["quota.release", "daily.release", "log.mark_rejected"]
    assert calls[2][2] == {
        "error_code": "user_unavailable",
        "error_message": "User is no longer active",
        "log_level": "INFO",
        "attempt_number": 0,
    }


def test_second_settlement_is_noop():
    calls: list = []
    accounting = _accounting(calls=calls)

    accounting.settle_failure(queued_ms=1, error_code="x", error_message="first")
    first_calls = list(calls)

    accounting.settle_failure(queued_ms=2, error_code="y", error_message="second")
    accounting.settle_success(queued_ms=3, final_cost=7, output_files=[])
    accounting.settle_rejected(error_code="z", error_message="third")
    accounting.settle_released()

    assert calls == first_calls


def test_settle_released_only_releases_without_log_write():
    calls: list = []
    accounting = _accounting(calls=calls)

    accounting.settle_released()

    assert [call[0] for call in calls] == ["quota.release", "daily.release"]
    assert accounting.settled


def test_settle_rejected_after_settle_released_writes_log_without_double_release():
    # 队列层释放额度（如队列满 / 无可用上游）后，service 层补写 rejected 日志的路径。
    calls: list = []
    accounting = _accounting(calls=calls)

    accounting.settle_released()
    accounting.settle_rejected(error_code="queue_full", error_message="Queue full, please retry later")

    assert [call[0] for call in calls] == ["quota.release", "daily.release", "log.mark_rejected"]


def test_retry_failure_keeps_reservation_until_final_settlement():
    calls: list = []
    accounting = _accounting(calls=calls)

    # 第一次尝试 429：旧日志行标记 failed，但预留保持。
    accounting.record_retry_failure(queued_ms=10, error_code="429", error_message="Too many requests", attempt_number=0)
    assert [call[0] for call in calls] == ["log.mark_failed"]
    assert not accounting.settled

    # 重试入队：插入新日志行。
    accounting.record_retry_attempt(attempt_number=1, upstream_id="opus-a")
    assert calls[-1] == ("log.insert_retry_attempt", "req-1", 1, "opus-a")

    # 重试成功：确认额度并把新行标记 success。
    accounting.settle_success(queued_ms=20, final_cost=7, output_files=[], is_retry_success=True, attempt_number=1)
    assert [call[0] for call in calls] == [
        "log.mark_failed",
        "log.insert_retry_attempt",
        "quota.confirm",
        "daily.confirm",
        "log.mark_success",
    ]
    assert calls[-1][2]["attempt_number"] == 1


def test_retry_failure_then_released_when_retry_abandoned():
    # 429 标记 failed 后放弃重试（超过最大次数 / 重试被取消）：只释放预留，不再写日志。
    calls: list = []
    accounting = _accounting(calls=calls)

    accounting.record_retry_failure(queued_ms=10, error_code="429", error_message="Too many requests", attempt_number=0)
    accounting.settle_released()

    assert [call[0] for call in calls] == ["log.mark_failed", "quota.release", "daily.release"]


def test_record_retry_attempt_failure_releases_reservation_and_reraises():
    calls: list = []
    accounting = RequestAccounting(
        quota_manager=RecordingQuota(calls),
        usage_logs=FailingRetryInsertUsageLogs(calls),
        request_id="req-1",
        user_id=42,
        estimated_cost=7,
        free_small_daily_limit_manager=RecordingDaily(calls),
        free_small_daily_reservation=_reservation(),
    )

    with pytest.raises(RuntimeError, match="retry log insert failed"):
        accounting.record_retry_attempt(attempt_number=1, upstream_id="opus-a")

    assert [call[0] for call in calls] == ["quota.release", "daily.release"]
    assert accounting.settled


def test_unmanaged_quota_and_missing_reservation_settle_is_log_only():
    calls: list = []
    accounting = _accounting(calls=calls, manage_quota=False, with_daily=False)

    accounting.settle_failure(queued_ms=1, error_code="x", error_message="no reservations")

    assert [call[0] for call in calls] == ["log.mark_failed"]


def test_unmanaged_quota_settle_released_touches_nothing():
    calls: list = []
    accounting = _accounting(calls=calls, manage_quota=False, with_daily=False)

    accounting.settle_released()

    assert calls == []
    assert accounting.settled


def test_daily_reservation_errors_are_swallowed():
    calls: list = []
    accounting = RequestAccounting(
        quota_manager=RecordingQuota(calls),
        usage_logs=RecordingUsageLogs(calls),
        request_id="req-1",
        user_id=42,
        estimated_cost=7,
        free_small_daily_limit_manager=FailingDaily(),
        free_small_daily_reservation=_reservation(),
    )

    accounting.settle_success(queued_ms=1, final_cost=7, output_files=[])

    assert [call[0] for call in calls] == ["quota.confirm", "log.mark_success"]
