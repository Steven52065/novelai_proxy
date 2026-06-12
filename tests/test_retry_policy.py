from app.retry_policy import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_VIP_RETRY_PRIORITY,
    RetryDecision,
    RetryPolicy,
)


def _policy(**overrides):
    params = {"queue_length_threshold": 3, "max_attempts": 5, "vip_retry_priority": -1}
    params.update(overrides)
    return RetryPolicy(**params)


def test_should_attempt_429_retry_within_threshold():
    policy = _policy(queue_length_threshold=3)
    assert policy.should_attempt_429_retry(0) is True
    assert policy.should_attempt_429_retry(3) is True


def test_should_attempt_429_retry_above_threshold():
    policy = _policy(queue_length_threshold=3)
    assert policy.should_attempt_429_retry(4) is False


def test_should_attempt_429_retry_negative_threshold_disables_retry():
    policy = _policy(queue_length_threshold=-1)
    assert policy.should_attempt_429_retry(0) is False


def test_should_attempt_429_retry_zero_threshold_only_empty_queue():
    policy = _policy(queue_length_threshold=0)
    assert policy.should_attempt_429_retry(0) is True
    assert policy.should_attempt_429_retry(1) is False


def test_decide_retry_normal_user_requeues_with_current_priority():
    policy = _policy(max_attempts=5, vip_retry_priority=-1)
    decision = policy.decide_retry(tier="normal", attempt_number=0, current_priority=10)
    assert decision == RetryDecision(
        should_retry=True,
        next_attempt_number=1,
        priority=10,
        immediate=False,
    )


def test_decide_retry_vip_uses_vip_priority_and_immediate_dispatch():
    policy = _policy(max_attempts=5, vip_retry_priority=-1)
    decision = policy.decide_retry(tier="vip", attempt_number=2, current_priority=0)
    assert decision == RetryDecision(
        should_retry=True,
        next_attempt_number=3,
        priority=-1,
        immediate=True,
    )


def test_decide_retry_gives_up_when_next_attempt_reaches_max():
    policy = _policy(max_attempts=5)
    # attempt_number 4 -> next is 5, which reaches the limit and gives up
    decision = policy.decide_retry(tier="normal", attempt_number=4, current_priority=10)
    assert decision.should_retry is False
    assert decision.next_attempt_number == 5


def test_decide_retry_last_allowed_attempt():
    policy = _policy(max_attempts=5)
    # attempt_number 3 -> next is 4 (< 5), still retried
    decision = policy.decide_retry(tier="normal", attempt_number=3, current_priority=10)
    assert decision.should_retry is True
    assert decision.next_attempt_number == 4


def test_decide_retry_vip_also_gives_up_at_limit():
    policy = _policy(max_attempts=2, vip_retry_priority=-1)
    decision = policy.decide_retry(tier="vip", attempt_number=1, current_priority=0)
    assert decision.should_retry is False


def test_policy_defaults_match_existing_constants():
    policy = RetryPolicy(queue_length_threshold=3)
    assert policy.max_attempts == DEFAULT_MAX_ATTEMPTS == 5
    assert policy.vip_retry_priority == DEFAULT_VIP_RETRY_PRIORITY == -1
