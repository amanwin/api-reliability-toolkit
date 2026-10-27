"""Tests for retry deadline budgeting."""

import pytest

from api_reliability import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    RetryConfigurationError,
    retry_call,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


def test_timeout_none_preserves_behavior_and_never_reads_clock():
    calls = []
    sleeps = []

    def clock():
        pytest.fail("clock must not be called when timeout is None")

    def operation():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("temporary")
        return "ok"

    assert retry_call(operation, base_delay=1, sleep=sleeps.append, clock=clock) == "ok"
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_budget_stops_retries_with_time_left_over():
    clock = FakeClock()
    calls = []

    def operation():
        calls.append(clock.now)
        raise ConnectionError("down")

    error = ConnectionError("down")

    def same_error():
        calls.append(clock.now)
        raise error

    with pytest.raises(ConnectionError) as excinfo:
        retry_call(
            same_error,
            max_attempts=5,
            retry_on=(ConnectionError,),
            base_delay=1,
            max_delay=100,
            timeout=5,
            clock=clock,
            sleep=clock.sleep,
        )

    assert excinfo.value is error
    assert len(calls) == 3
    assert clock.sleeps == [1, 2]
    assert clock.now == 1003


def test_timeout_zero_still_runs_one_attempt_and_does_not_sleep():
    calls = []
    error = ConnectionError("first attempt")

    def operation():
        calls.append(1)
        raise error

    with pytest.raises(ConnectionError) as excinfo:
        retry_call(
            operation,
            max_attempts=4,
            retry_on=(ConnectionError,),
            timeout=0,
            clock=lambda: 0.0,
            sleep=lambda _: pytest.fail("must not sleep"),
        )

    assert calls == [1]
    assert excinfo.value is error


def test_operation_duration_counts_against_deadline():
    clock = FakeClock()
    calls = []
    error = ConnectionError("down")

    def operation():
        calls.append(1)
        clock.advance(3)
        raise error

    with pytest.raises(ConnectionError) as excinfo:
        retry_call(
            operation,
            max_attempts=4,
            retry_on=(ConnectionError,),
            base_delay=1,
            timeout=5,
            clock=clock,
            sleep=clock.sleep,
        )

    assert excinfo.value is error
    assert len(calls) == 2
    assert clock.sleeps == [1]
    assert clock.now == 1007


def test_delay_that_lands_exactly_on_deadline_does_not_retry():
    clock = FakeClock()
    calls = []
    error = ConnectionError("boundary")

    def operation():
        calls.append(1)
        raise error

    with pytest.raises(ConnectionError) as excinfo:
        retry_call(
            operation,
            max_attempts=3,
            retry_on=(ConnectionError,),
            base_delay=1,
            timeout=1,
            clock=clock,
            sleep=clock.sleep,
        )

    assert excinfo.value is error
    assert len(calls) == 1
    assert clock.sleeps == []


def test_jittered_delay_uses_jittered_value_for_deadline_check():
    clock = FakeClock()
    calls = []

    def operation():
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("temporary")
        return "ok"

    assert retry_call(
        operation,
        max_attempts=2,
        retry_on=(ConnectionError,),
        base_delay=2,
        timeout=1.5,
        jitter=True,
        random_fn=lambda: 0.5,
        clock=clock,
        sleep=clock.sleep,
    ) == "ok"
    assert len(calls) == 2
    assert clock.sleeps == [1.0]


def test_invalid_timeout_values_are_rejected_before_operation():
    for timeout in (-1, float("nan"), float("inf"), float("-inf"), True, "5"):
        calls = []
        with pytest.raises(RetryConfigurationError):
            retry_call(lambda: calls.append(1), timeout=timeout)
        assert calls == []


def test_non_callable_clock_is_rejected_even_without_timeout():
    calls = []
    with pytest.raises(RetryConfigurationError):
        retry_call(lambda: calls.append(1), clock=None)
    assert calls == []


def test_non_retryable_exception_is_not_changed_by_deadline():
    error = ValueError("business error")
    with pytest.raises(ValueError) as excinfo:
        retry_call(
            lambda: (_ for _ in ()).throw(error),
            retry_on=(ConnectionError,),
            timeout=5,
            clock=lambda: 0.0,
        )
    assert excinfo.value is error


def test_breaker_failure_count_is_preserved_when_deadline_stops_retries():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=10, clock=clock)
    calls = []

    def operation():
        calls.append(1)
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        retry_call(
            operation,
            max_attempts=5,
            retry_on=(ConnectionError,),
            base_delay=1,
            timeout=2,
            clock=clock,
            sleep=clock.sleep,
            circuit_breaker=breaker,
        )

    assert len(calls) == 2
    assert breaker.failure_count == 2
    assert breaker.state is CircuitState.CLOSED
    assert clock.sleeps == [1]


def test_open_breaker_rejection_is_not_retried_by_deadline_logic():
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
    with pytest.raises(ConnectionError):
        breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("open it")))
    assert breaker.state is CircuitState.OPEN
    calls = []

    with pytest.raises(CircuitOpenError):
        retry_call(
            lambda: calls.append(1),
            max_attempts=3,
            timeout=10,
            circuit_breaker=breaker,
            sleep=lambda _: pytest.fail("must not sleep"),
        )
    assert calls == []


def test_timeout_validation_does_not_call_clock_before_rejecting_bad_timeout():
    def clock():
        pytest.fail("invalid timeout must be rejected before reading clock")

    with pytest.raises(RetryConfigurationError):
        retry_call(lambda: None, timeout=float("inf"), clock=clock)
