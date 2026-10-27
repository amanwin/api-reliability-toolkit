"""Interaction between ``retry_call`` and ``CircuitBreaker``."""

import threading

import pytest

from api_reliability import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    RetryConfigurationError,
    retry_call,
)

THREAD_TIMEOUT = 5.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def no_sleep(_delay: float) -> None:
    pytest.fail("sleep should not be called")


def open_breaker(breaker: CircuitBreaker) -> None:
    def failing_operation():
        raise ConnectionError("down")

    while breaker.state is CircuitState.CLOSED:
        with pytest.raises(ConnectionError):
            breaker.call(failing_operation)
    assert breaker.state is CircuitState.OPEN


def test_success_through_breaker_returns_result():
    breaker = CircuitBreaker(failure_threshold=2)
    sleeps = []

    result = retry_call(lambda: "ok", circuit_breaker=breaker, sleep=sleeps.append)

    assert result == "ok"
    assert sleeps == []
    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 0


def test_each_failed_attempt_counts_as_one_breaker_failure():
    breaker = CircuitBreaker(failure_threshold=5)
    calls = []
    sleeps = []

    def operation():
        calls.append("call")
        if len(calls) < 3:
            raise ConnectionError("temporary")
        return "recovered"

    result = retry_call(
        operation,
        max_attempts=3,
        retry_on=(ConnectionError,),
        base_delay=0.1,
        max_delay=1,
        sleep=sleeps.append,
        circuit_breaker=breaker,
    )

    assert result == "recovered"
    assert sleeps == [0.1, 0.2]
    # Two failures were counted, then the success reset the count.
    assert breaker.failure_count == 0
    assert breaker.state is CircuitState.CLOSED


def test_retries_stop_as_soon_as_breaker_opens():
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
    calls = []
    sleeps = []

    def operation():
        calls.append("call")
        raise ConnectionError("down")

    with pytest.raises(CircuitOpenError) as excinfo:
        retry_call(
            operation,
            max_attempts=5,
            retry_on=(ConnectionError,),
            base_delay=0.1,
            max_delay=1,
            sleep=sleeps.append,
            circuit_breaker=breaker,
        )

    # Attempts 1 and 2 reached the operation and opened the breaker; attempt 3
    # was rejected without a call and without any further sleeping.
    assert len(calls) == 2
    assert sleeps == [0.1, 0.2]
    assert excinfo.value.breaker is breaker
    assert breaker.state is CircuitState.OPEN
    assert breaker.failure_count == 2


def test_open_breaker_rejects_before_first_attempt():
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
    open_breaker(breaker)

    calls = []
    with pytest.raises(CircuitOpenError):
        retry_call(
            lambda: calls.append("call"),
            max_attempts=3,
            circuit_breaker=breaker,
            sleep=lambda _: pytest.fail("sleep should not be called"),
        )
    assert calls == []


def test_breaker_rejection_is_not_retried_even_when_retry_on_matches_it():
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
    calls = []
    sleeps = []

    def operation():
        calls.append("call")
        raise ConnectionError("down")

    with pytest.raises(CircuitOpenError):
        retry_call(
            operation,
            max_attempts=4,
            retry_on=(Exception,),  # would match CircuitOpenError
            sleep=sleeps.append,
            circuit_breaker=breaker,
        )

    assert len(calls) == 1
    assert len(sleeps) == 1


def test_final_attempt_failure_is_reraised_and_counted():
    breaker = CircuitBreaker(failure_threshold=10)
    calls = []

    def operation():
        calls.append("call")
        raise ConnectionError("still down")

    with pytest.raises(ConnectionError, match="still down"):
        retry_call(
            operation,
            max_attempts=3,
            retry_on=(ConnectionError,),
            sleep=lambda _: None,
            circuit_breaker=breaker,
        )

    assert len(calls) == 3
    assert breaker.failure_count == 3
    assert breaker.state is CircuitState.CLOSED


def test_non_retryable_exception_propagates_and_breaker_applies_its_own_filter():
    breaker = CircuitBreaker(failure_threshold=1, failure_on=(ConnectionError,))
    calls = []

    def operation():
        calls.append("call")
        raise ValueError("not retryable, not a breaker failure")

    with pytest.raises(ValueError):
        retry_call(
            operation,
            max_attempts=3,
            retry_on=(ConnectionError,),
            sleep=lambda _: pytest.fail("sleep should not be called"),
            circuit_breaker=breaker,
        )

    assert len(calls) == 1
    assert breaker.failure_count == 0
    assert breaker.state is CircuitState.CLOSED


def test_retry_on_and_failure_on_are_independent():
    """An exception can be retried without counting as a breaker failure."""
    breaker = CircuitBreaker(failure_threshold=1, failure_on=(ConnectionError,))
    calls = []

    def operation():
        calls.append("call")
        if len(calls) < 3:
            raise TimeoutError("retryable but not a breaker failure")
        return "ok"

    result = retry_call(
        operation,
        max_attempts=3,
        retry_on=(TimeoutError,),
        sleep=lambda _: None,
        circuit_breaker=breaker,
    )

    assert result == "ok"
    assert len(calls) == 3
    assert breaker.state is CircuitState.CLOSED


def test_retry_can_resume_after_recovery_timeout():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30, clock=clock)
    calls = []

    def operation():
        calls.append("call")
        if len(calls) == 1:
            raise ConnectionError("down")
        return "back"

    with pytest.raises(CircuitOpenError):
        retry_call(
            operation,
            max_attempts=3,
            retry_on=(ConnectionError,),
            sleep=lambda _: None,
            circuit_breaker=breaker,
        )
    assert len(calls) == 1

    clock.now += 30
    result = retry_call(
        operation,
        max_attempts=3,
        retry_on=(ConnectionError,),
        sleep=lambda _: None,
        circuit_breaker=breaker,
    )
    assert result == "back"
    assert len(calls) == 2
    assert breaker.state is CircuitState.CLOSED


def test_rejection_from_a_different_breaker_is_retried_per_retry_on():
    outer = CircuitBreaker(failure_threshold=10)
    inner = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
    open_breaker(inner)

    calls = []

    def operation():
        calls.append("call")
        return inner.call(lambda: "never")

    with pytest.raises(CircuitOpenError) as excinfo:
        retry_call(
            operation,
            max_attempts=3,
            retry_on=(Exception,),
            sleep=lambda _: None,
            circuit_breaker=outer,
        )

    # The inner breaker's rejection is an ordinary operation exception for
    # the outer breaker and the retry loop.
    assert excinfo.value.breaker is inner
    assert len(calls) == 3
    assert outer.failure_count == 3


def test_without_breaker_circuit_open_error_from_operation_follows_retry_on():
    """Backward compatibility: with no breaker, retry_on alone decides."""
    calls = []

    def operation():
        calls.append("call")
        if len(calls) < 2:
            raise CircuitOpenError()
        return "ok"

    result = retry_call(operation, max_attempts=2, sleep=lambda _: None)
    assert result == "ok"
    assert len(calls) == 2


def test_invalid_circuit_breaker_argument_is_rejected():
    with pytest.raises(RetryConfigurationError):
        retry_call(lambda: None, circuit_breaker="breaker")

    with pytest.raises(RetryConfigurationError):
        retry_call(lambda: None, circuit_breaker=object())


def test_existing_signature_is_unchanged_without_breaker():
    """Positional/keyword usage from Milestone 1 keeps working."""
    sleeps = []
    result = retry_call(
        lambda: "ok",
        max_attempts=3,
        retry_on=(ConnectionError,),
        base_delay=0.1,
        max_delay=1.0,
        jitter=False,
        sleep=sleeps.append,
        random_fn=lambda: 0.5,
    )
    assert result == "ok"
    assert sleeps == []


# Additional coverage -------------------------------------------------------


def test_success_returns_the_same_object_after_exactly_one_call():
    breaker = CircuitBreaker(failure_threshold=1)
    payload = {"id": 1}
    calls = []

    def operation():
        calls.append("call")
        return payload

    result = retry_call(operation, max_attempts=5, circuit_breaker=breaker, sleep=no_sleep)

    assert result is payload
    assert calls == ["call"]
    assert breaker.state is CircuitState.CLOSED


def test_breaker_failure_count_increments_once_per_failed_attempt():
    breaker = CircuitBreaker(failure_threshold=10)
    observed_counts = []

    def operation():
        observed_counts.append(breaker.failure_count)
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        retry_call(
            operation,
            max_attempts=4,
            retry_on=(ConnectionError,),
            sleep=lambda _: None,
            circuit_breaker=breaker,
        )

    # Count seen at the start of each attempt: one more than the previous.
    assert observed_counts == [0, 1, 2, 3]
    assert breaker.failure_count == 4


def test_exception_counted_by_breaker_but_not_retried_propagates_immediately():
    """The fourth quadrant: in failure_on, not in retry_on."""
    breaker = CircuitBreaker(failure_threshold=5, failure_on=(ValueError,))
    calls = []

    def operation():
        calls.append("call")
        raise ValueError("counted, not retried")

    with pytest.raises(ValueError, match="counted, not retried"):
        retry_call(
            operation,
            max_attempts=3,
            retry_on=(ConnectionError,),
            sleep=no_sleep,
            circuit_breaker=breaker,
        )

    assert len(calls) == 1
    assert breaker.failure_count == 1


def test_final_retryable_exception_is_the_same_object_with_breaker():
    breaker = CircuitBreaker(failure_threshold=10)
    error = ConnectionError("final")

    def operation():
        raise error

    with pytest.raises(ConnectionError) as excinfo:
        retry_call(
            operation,
            max_attempts=2,
            retry_on=(ConnectionError,),
            sleep=lambda _: None,
            circuit_breaker=breaker,
        )

    assert excinfo.value is error
    assert breaker.failure_count == 2


def test_max_attempts_one_with_breaker_does_not_sleep_and_counts_failure():
    breaker = CircuitBreaker(failure_threshold=2)

    with pytest.raises(ConnectionError):
        retry_call(
            lambda: (_ for _ in ()).throw(ConnectionError("down")),
            max_attempts=1,
            retry_on=(ConnectionError,),
            sleep=no_sleep,
            circuit_breaker=breaker,
        )

    assert breaker.failure_count == 1
    assert breaker.state is CircuitState.CLOSED


def test_backoff_is_exponential_and_capped_with_breaker():
    breaker = CircuitBreaker(failure_threshold=100)
    sleeps = []

    def operation():
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        retry_call(
            operation,
            max_attempts=5,
            retry_on=(ConnectionError,),
            base_delay=1,
            max_delay=4,
            sleep=sleeps.append,
            circuit_breaker=breaker,
        )

    assert sleeps == [1, 2, 4, 4]
    assert breaker.failure_count == 5


def test_jitter_scales_each_delay_with_breaker():
    breaker = CircuitBreaker(failure_threshold=100)
    sleeps = []
    factors = iter([0.5, 0.25])

    def operation():
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        retry_call(
            operation,
            max_attempts=3,
            retry_on=(ConnectionError,),
            base_delay=1,
            max_delay=4,
            jitter=True,
            random_fn=lambda: next(factors),
            sleep=sleeps.append,
            circuit_breaker=breaker,
        )

    assert sleeps == [0.5, 0.5]  # 1 * 0.5, 2 * 0.25


def test_invalid_jitter_value_is_rejected_with_breaker():
    breaker = CircuitBreaker(failure_threshold=100)

    def operation():
        raise ConnectionError("down")

    with pytest.raises(RetryConfigurationError):
        retry_call(
            operation,
            max_attempts=2,
            retry_on=(ConnectionError,),
            jitter=True,
            random_fn=lambda: 1.0,
            sleep=no_sleep,
            circuit_breaker=breaker,
        )

    # The first attempt ran and was counted before jitter validation failed.
    assert breaker.failure_count == 1


def test_failed_trial_inside_retry_reopens_breaker_and_stops_retrying():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10, clock=clock)
    open_breaker(breaker)
    clock.now += 10
    assert breaker.state is CircuitState.HALF_OPEN

    calls = []
    sleeps = []

    def operation():
        calls.append("call")
        raise ConnectionError("still down")

    with pytest.raises(CircuitOpenError) as excinfo:
        retry_call(
            operation,
            max_attempts=3,
            retry_on=(ConnectionError,),
            base_delay=0.1,
            max_delay=1,
            sleep=sleeps.append,
            circuit_breaker=breaker,
        )

    # The single trial attempt failed and re-opened the circuit; the second
    # attempt was rejected with no further calls or sleeps.
    assert excinfo.value.breaker is breaker
    assert len(calls) == 1
    assert sleeps == [0.1]
    assert breaker.state is CircuitState.OPEN

    # After the restarted timeout, a fresh retry_call can recover the breaker.
    clock.now += 10
    assert retry_call(lambda: "back", circuit_breaker=breaker, sleep=no_sleep) == "back"
    assert breaker.state is CircuitState.CLOSED


def test_half_open_trial_limit_rejection_propagates_without_attempts_or_sleeps():
    clock = FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout=1, half_open_max_calls=1, clock=clock
    )
    open_breaker(breaker)
    clock.now += 1
    assert breaker.state is CircuitState.HALF_OPEN

    started = threading.Event()
    release = threading.Event()
    results = {}

    def hold_trial_permit():
        def blocking_operation():
            started.set()
            assert release.wait(THREAD_TIMEOUT)
            return "trial ok"

        results["trial"] = breaker.call(blocking_operation)

    worker = threading.Thread(target=hold_trial_permit)
    worker.start()
    calls = []
    try:
        assert started.wait(THREAD_TIMEOUT)
        with pytest.raises(CircuitOpenError, match="half-open") as excinfo:
            retry_call(
                lambda: calls.append("call"),
                max_attempts=5,
                sleep=no_sleep,
                circuit_breaker=breaker,
            )
        assert excinfo.value.breaker is breaker
    finally:
        release.set()
        worker.join(THREAD_TIMEOUT)

    assert not worker.is_alive()
    assert calls == []
    assert results["trial"] == "trial ok"
    assert breaker.state is CircuitState.CLOSED


def test_nested_breaker_rejection_not_counted_when_outer_failure_on_excludes_it():
    inner = CircuitBreaker(failure_threshold=1, recovery_timeout=100)
    open_breaker(inner)
    outer = CircuitBreaker(failure_threshold=10, failure_on=(ConnectionError,))
    calls = []

    def operation():
        calls.append("call")
        return inner.call(lambda: "never")

    with pytest.raises(CircuitOpenError) as excinfo:
        retry_call(
            operation,
            max_attempts=3,
            retry_on=(CircuitOpenError,),
            sleep=lambda _: None,
            circuit_breaker=outer,
        )

    assert excinfo.value.breaker is inner
    assert len(calls) == 3  # retried per retry_on ...
    assert outer.failure_count == 0  # ... but not counted per failure_on
    assert outer.state is CircuitState.CLOSED


def test_nested_breaker_rejection_not_retried_when_retry_on_excludes_it():
    inner = CircuitBreaker(failure_threshold=1, recovery_timeout=100)
    open_breaker(inner)
    outer = CircuitBreaker(failure_threshold=10)
    calls = []

    def operation():
        calls.append("call")
        return inner.call(lambda: "never")

    with pytest.raises(CircuitOpenError) as excinfo:
        retry_call(
            operation,
            max_attempts=3,
            retry_on=(ConnectionError,),
            sleep=no_sleep,
            circuit_breaker=outer,
        )

    assert excinfo.value.breaker is inner
    assert len(calls) == 1
    assert outer.failure_count == 1  # counted by the default failure_on


def test_milestone_one_positional_operation_and_defaults_still_work():
    calls = []

    def operation():
        calls.append("call")
        if len(calls) < 2:
            raise ConnectionError("temporary")
        return "ok"

    assert retry_call(operation, sleep=lambda _: None) == "ok"
    assert len(calls) == 2
