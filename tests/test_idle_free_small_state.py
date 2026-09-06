from __future__ import annotations

from app.idle_free_small import IdleFreeSmallTracker


def _tracker(*, threshold: float = 50, seconds: float = 30) -> tuple[IdleFreeSmallTracker, list[float]]:
    clock_values = [0.0]
    tracker = IdleFreeSmallTracker(
        occupancy_threshold_percent=threshold,
        min_idle_seconds=seconds,
        clock=lambda: clock_values[0],
    )
    return tracker, clock_values


def test_requires_start_and_honors_exact_min_idle_boundary():
    tracker, clock = _tracker(seconds=30)
    token = object()
    tracker.attach("a", token)
    assert tracker.is_idle() is False

    tracker.start()
    clock[0] = 29.9
    assert tracker.is_idle() is False
    clock[0] = 30.0
    assert tracker.is_idle() is True


def test_busy_state_interrupts_idle_window():
    tracker, clock = _tracker(threshold=50, seconds=30)
    token_a = object()
    token_b = object()
    tracker.attach("a", token_a)
    tracker.attach("b", token_b)
    tracker.start()
    clock[0] = 10.0
    tracker.record_started(token_a, "a")
    clock[0] = 50.0
    assert tracker.is_idle() is False

    clock[0] = 50.0
    tracker.record_finished(token_a)
    clock[0] = 79.9
    assert tracker.is_idle() is False
    clock[0] = 80.0
    assert tracker.is_idle() is True


def test_threshold_is_strictly_below_and_last_request_can_cross():
    tracker, clock = _tracker(threshold=60, seconds=30)
    tokens = [object() for _ in range(4)]
    for upstream_id, token in zip(("a", "b", "c", "d"), tokens):
        tracker.attach(upstream_id, token)
    tracker.start()
    clock[0] = 5.0
    tracker.record_started(tokens[0], "a")
    tracker.record_started(tokens[1], "b")
    clock[0] = 40.0
    assert tracker.is_idle() is True
    clock[0] = 40.0
    tracker.record_started(tokens[2], "c")
    clock[0] = 70.0
    assert tracker.is_idle() is False


def test_allowed_upstream_subset_uses_its_own_denominator():
    tracker, clock = _tracker(threshold=50, seconds=30)
    token_a = object()
    token_b = object()
    tracker.attach("a", token_a)
    tracker.attach("b", token_b)
    tracker.start()
    clock[0] = 5.0
    tracker.record_started(token_a, "a")
    clock[0] = 40.0
    assert tracker.is_idle(["a"]) is False
    assert tracker.is_idle(["b"]) is True
    assert tracker.is_idle([]) is False
    assert tracker.is_idle(["missing"]) is False


def test_single_upstream_can_be_idle_under_50_percent():
    tracker, clock = _tracker(threshold=50, seconds=30)
    token = object()
    tracker.attach("only", token)
    tracker.start()
    clock[0] = 30.0
    assert tracker.is_idle() is True


def test_min_idle_zero_checks_current_state_only():
    tracker, clock = _tracker(threshold=50, seconds=0)
    token = object()
    tracker.attach("a", token)
    tracker.start()
    assert tracker.is_idle() is True
    tracker.record_started(token, "a")
    assert tracker.is_idle() is False


def test_old_worker_finish_does_not_clear_new_worker_with_same_id():
    tracker, clock = _tracker(threshold=50, seconds=30)
    old = object()
    new = object()
    tracker.attach("a", old)
    tracker.start()
    tracker.record_started(old, "a")
    tracker.detach("a", old)
    tracker.attach("a", new)
    tracker.record_started(new, "a")
    tracker.record_finished(old)
    assert tracker.active_workers["a"] is new
    assert tracker.running_sources[new] == "a"
    assert old not in tracker.running_sources


def test_attach_and_detach_record_only_real_state_changes():
    tracker, clock = _tracker(seconds=30)
    token = object()
    tracker.start()
    clock[0] = 1.0
    tracker.attach("a", token)
    clock[0] = 1.0
    tracker.attach("a", token)
    clock[0] = 2.0
    tracker.detach("a", token)
    assert [len(event.enabled_ids) for event in tracker.events] == [0, 1, 0]


def test_natural_concurrency_follows_strict_threshold():
    tracker, clock = _tracker(threshold=50, seconds=30)
    tokens = [object() for _ in range(4)]
    for upstream_id, token in zip(("a", "b", "c", "d"), tokens):
        tracker.attach(upstream_id, token)
    tracker.start()
    tracker.record_started(tokens[0], "a")
    tracker.record_started(tokens[1], "b")
    clock[0] = 30.0
    assert tracker.is_idle() is False
    tracker.record_finished(tokens[1])
    clock[0] = 60.0
    assert tracker.is_idle() is True

    threshold_sixty, clock_sixty = _tracker(threshold=60, seconds=30)
    tokens_sixty = [object() for _ in range(4)]
    for upstream_id, token in zip(("a", "b", "c", "d"), tokens_sixty):
        threshold_sixty.attach(upstream_id, token)
    threshold_sixty.start()
    threshold_sixty.record_started(tokens_sixty[0], "a")
    threshold_sixty.record_started(tokens_sixty[1], "b")
    clock_sixty[0] = 30.0
    assert threshold_sixty.is_idle() is True
    threshold_sixty.record_started(tokens_sixty[2], "c")
    assert threshold_sixty.is_idle() is False
