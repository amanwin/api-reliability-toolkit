import threading

import pytest

from api_reliability import (
    CircuitBreaker,
    CircuitBreakerConfigurationError,
    CircuitOpenError,
    CircuitState,
)

THREAD_TIMEOUT = 5.0


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def fail(exc: BaseException | None = None):
    raise exc if exc is not None else ConnectionError("down")


def record_failure(breaker: CircuitBreaker, exc: BaseException | None = None):
    if exc is None:
        exc = ConnectionError("down")
    with pytest.raises(type(exc)):
        breaker.call(lambda: fail(exc))


def open_breaker(breaker: CircuitBreaker, threshold: int) -> None:
    for _ in range(threshold):
        record_failure(breaker)
    assert breaker.state is CircuitState.OPEN


# State transitions ----------------------------------------------------------


def test_starts_closed_and_passes_through_results():
    breaker = CircuitBreaker()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 0


def test_operation_exception_propagates_unchanged():
    breaker = CircuitBreaker(failure_threshold=3)
    with pytest.raises(ConnectionError, match="down"):
        breaker.call(lambda: fail(ConnectionError("down")))
    assert breaker.failure_count == 1
    assert breaker.state is CircuitState.CLOSED


def test_opens_after_consecutive_failures_reach_threshold():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=10, clock=clock)
    calls = []

    record_failure(breaker)
    record_failure(breaker)
    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 2

    record_failure(breaker)
    assert breaker.state is CircuitState.OPEN
    assert breaker.failure_count == 3

    with pytest.raises(CircuitOpenError) as excinfo:
        breaker.call(lambda: calls.append("called"))
    assert calls == []
    assert excinfo.value.breaker is breaker


def test_success_resets_consecutive_failure_count():
    breaker = CircuitBreaker(failure_threshold=2)
    record_failure(breaker)
    breaker.call(lambda: "ok")
    assert breaker.failure_count == 0
    record_failure(breaker)
    assert breaker.state is CircuitState.CLOSED


def test_exceptions_outside_failure_on_are_ignored():
    breaker = CircuitBreaker(failure_threshold=1, failure_on=(ConnectionError,))
    with pytest.raises(ValueError):
        breaker.call(lambda: fail(ValueError("business error")))
    assert breaker.failure_count == 0
    assert breaker.state is CircuitState.CLOSED


def test_failure_on_subclasses_count_as_failures():
    breaker = CircuitBreaker(failure_threshold=1, failure_on=(OSError,))
    record_failure(breaker, ConnectionRefusedError("refused"))
    assert breaker.state is CircuitState.OPEN


# Recovery -------------------------------------------------------------------


def test_stays_open_until_recovery_timeout_elapses():
    clock = FakeClock(start=100.0)
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=10, clock=clock)
    open_breaker(breaker, 1)

    clock.advance(9.999)
    assert breaker.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: "ok")

    clock.advance(0.001)
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.failure_count == 0


def test_half_open_success_closes_circuit():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=5, clock=clock)
    open_breaker(breaker, 1)
    clock.advance(5)

    assert breaker.call(lambda: "recovered") == "recovered"
    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 0


def test_half_open_failure_reopens_and_restarts_timeout():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=5, clock=clock)
    open_breaker(breaker, 1)
    clock.advance(5)
    assert breaker.state is CircuitState.HALF_OPEN

    clock.advance(3)
    record_failure(breaker)
    assert breaker.state is CircuitState.OPEN

    clock.advance(4)  # 4s after re-opening: still short of the 5s timeout
    assert breaker.state is CircuitState.OPEN
    clock.advance(1)
    assert breaker.state is CircuitState.HALF_OPEN


def test_recovery_transition_happens_on_call_without_reading_state():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=5, clock=clock)
    open_breaker(breaker, 1)
    clock.advance(5)
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.state is CircuitState.CLOSED


def test_success_threshold_requires_consecutive_trial_successes():
    clock = FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout=1, success_threshold=2, clock=clock
    )
    open_breaker(breaker, 1)
    clock.advance(1)

    breaker.call(lambda: "one")
    assert breaker.state is CircuitState.HALF_OPEN
    breaker.call(lambda: "two")
    assert breaker.state is CircuitState.CLOSED

    # A trial failure resets progress toward success_threshold.
    open_breaker(breaker, 1)
    clock.advance(1)
    breaker.call(lambda: "one")
    record_failure(breaker)
    assert breaker.state is CircuitState.OPEN
    clock.advance(1)
    breaker.call(lambda: "one again")
    assert breaker.state is CircuitState.HALF_OPEN


def test_zero_recovery_timeout_moves_to_half_open_immediately():
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0)
    record_failure(breaker)
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.state is CircuitState.CLOSED


def test_failure_count_applies_timed_transition_consistently_with_state():
    """Regression: failure_count must not depend on whether state was read first."""
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=5, clock=clock)
    open_breaker(breaker, 2)
    assert breaker.failure_count == 2

    clock.advance(5)
    # Read failure_count *before* state: the recovery timeout has elapsed, so
    # the breaker is effectively HALF_OPEN and the count must already be cleared.
    assert breaker.failure_count == 0
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.failure_count == 0


def test_reset_forces_closed_state():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=100, clock=clock)
    open_breaker(breaker, 1)

    breaker.reset()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 0
    assert breaker.call(lambda: "ok") == "ok"


def test_half_open_ignored_exception_releases_trial_permit_without_closing():
    clock = FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1,
        recovery_timeout=1,
        half_open_max_calls=1,
        failure_on=(ConnectionError,),
        clock=clock,
    )
    open_breaker(breaker, 1)
    clock.advance(1)

    with pytest.raises(ValueError):
        breaker.call(lambda: fail(ValueError("ignored")))
    assert breaker.state is CircuitState.HALF_OPEN

    # The permit was released, so the next trial call is admitted.
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.state is CircuitState.CLOSED


def test_base_exception_releases_trial_permit_and_propagates():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=1, clock=clock)
    open_breaker(breaker, 1)
    clock.advance(1)

    with pytest.raises(KeyboardInterrupt):
        breaker.call(lambda: fail(KeyboardInterrupt()))
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.state is CircuitState.CLOSED


def test_stale_result_from_previous_state_is_ignored():
    """A call admitted while CLOSED must not affect a later HALF_OPEN epoch."""
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=1, clock=clock)

    def slow_success():
        # While this call is "in flight", another call opens the breaker and
        # the timeout elapses, moving it to HALF_OPEN.
        record_failure(breaker)
        clock.advance(1)
        assert breaker.state is CircuitState.HALF_OPEN
        return "stale"

    assert breaker.call(slow_success) == "stale"
    # The stale success did not close the circuit.
    assert breaker.state is CircuitState.HALF_OPEN


# Invalid configuration ------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"failure_threshold": 0},
        {"failure_threshold": -1},
        {"failure_threshold": True},
        {"failure_threshold": 1.5},
        {"failure_threshold": "3"},
        {"recovery_timeout": -0.1},
        {"recovery_timeout": float("nan")},
        {"recovery_timeout": True},
        {"recovery_timeout": "10"},
        {"half_open_max_calls": 0},
        {"half_open_max_calls": 2.0},
        {"success_threshold": 0},
        {"success_threshold": False},
        {"failure_on": ()},
        {"failure_on": [ConnectionError]},
        {"failure_on": (str,)},
        {"failure_on": (ConnectionError(),)},
        {"failure_on": (ConnectionError, "TimeoutError")},
        {"failure_on": (BaseException,)},
        {"half_open_max_calls": -1},
        {"success_threshold": "2"},
        {"recovery_timeout": None},
        {"clock": None},
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(CircuitBreakerConfigurationError):
        CircuitBreaker(**kwargs)


def test_configuration_error_is_a_value_error():
    assert issubclass(CircuitBreakerConfigurationError, ValueError)


def test_non_callable_operation_is_rejected():
    with pytest.raises(CircuitBreakerConfigurationError):
        CircuitBreaker().call("not callable")


# Concurrency ----------------------------------------------------------------


def test_half_open_admits_only_configured_number_of_concurrent_trials():
    clock = FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout=1, half_open_max_calls=1, clock=clock
    )
    open_breaker(breaker, 1)
    clock.advance(1)

    started = threading.Event()
    release = threading.Event()
    results = {}

    def trial():
        def blocking_operation():
            started.set()
            assert release.wait(THREAD_TIMEOUT)
            return "trial ok"

        results["trial"] = breaker.call(blocking_operation)

    worker = threading.Thread(target=trial)
    worker.start()
    try:
        assert started.wait(THREAD_TIMEOUT)
        # The single trial permit is held by the worker: reject concurrently.
        assert breaker.state is CircuitState.HALF_OPEN
        with pytest.raises(CircuitOpenError, match="half-open"):
            breaker.call(lambda: "should not run")
    finally:
        release.set()
        worker.join(THREAD_TIMEOUT)

    assert not worker.is_alive()
    assert results["trial"] == "trial ok"
    assert breaker.state is CircuitState.CLOSED


def test_half_open_max_calls_greater_than_one():
    clock = FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1,
        recovery_timeout=1,
        half_open_max_calls=2,
        success_threshold=2,
        clock=clock,
    )
    open_breaker(breaker, 1)
    clock.advance(1)

    both_started = threading.Barrier(3)  # two workers + the test thread
    release = threading.Event()
    outcomes = []

    def worker():
        def blocking_operation():
            both_started.wait(THREAD_TIMEOUT)
            assert release.wait(THREAD_TIMEOUT)
            return "ok"

        outcomes.append(breaker.call(blocking_operation))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        both_started.wait(THREAD_TIMEOUT)
        # Two permits are held; a third concurrent call is rejected.
        with pytest.raises(CircuitOpenError):
            breaker.call(lambda: "third")
    finally:
        release.set()
        for thread in threads:
            thread.join(THREAD_TIMEOUT)

    assert all(not thread.is_alive() for thread in threads)
    assert outcomes == ["ok", "ok"]
    assert breaker.state is CircuitState.CLOSED


def test_concurrent_failures_never_exceed_threshold():
    """Many threads failing at once open the breaker exactly once."""
    threshold = 5
    workers = 32
    breaker = CircuitBreaker(failure_threshold=threshold, recovery_timeout=1000)
    start = threading.Barrier(workers)
    lock = threading.Lock()
    counts = {"executed": 0, "rejected": 0}

    def worker():
        start.wait(THREAD_TIMEOUT)

        def failing_operation():
            with lock:
                counts["executed"] += 1
            raise ConnectionError("down")

        try:
            breaker.call(failing_operation)
        except CircuitOpenError:
            with lock:
                counts["rejected"] += 1
        except ConnectionError:
            pass

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(THREAD_TIMEOUT)

    assert all(not thread.is_alive() for thread in threads)
    assert counts["executed"] + counts["rejected"] == workers
    assert counts["executed"] >= threshold
    assert breaker.state is CircuitState.OPEN
    # Stale failures from calls admitted before opening are discarded.
    assert breaker.failure_count == threshold


def test_concurrent_half_open_trials_agree_on_single_winner_state():
    """With one permit, exactly one of many racing callers runs the trial."""
    clock = FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout=1, half_open_max_calls=1, clock=clock
    )
    open_breaker(breaker, 1)
    clock.advance(1)

    workers = 16
    start = threading.Barrier(workers)
    release = threading.Event()
    lock = threading.Lock()
    counts = {"executed": 0, "rejected": 0}

    def worker():
        start.wait(THREAD_TIMEOUT)

        def trial_operation():
            with lock:
                counts["executed"] += 1
            assert release.wait(THREAD_TIMEOUT)
            return "ok"

        try:
            breaker.call(trial_operation)
        except CircuitOpenError:
            with lock:
                counts["rejected"] += 1
                if counts["rejected"] == workers - 1:
                    release.set()

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(THREAD_TIMEOUT)

    assert all(not thread.is_alive() for thread in threads)
    assert counts == {"executed": 1, "rejected": workers - 1}
    assert breaker.state is CircuitState.CLOSED


def test_admission_checks_are_mutually_exclusive():
    """Another thread's admission check blocks while one is in progress.

    The injected clock runs inside the admission critical section, so the
    callback can observe whether a second thread is able to enter it.
    """
    inside_critical_section = threading.Event()
    other_finished = threading.Event()
    observations = []
    thread_role = threading.local()

    def clock():
        if getattr(thread_role, "probe", False) and not inside_critical_section.is_set():
            inside_critical_section.set()
            # With correct locking the other thread cannot complete its
            # admission check until this callback returns.
            observations.append(other_finished.wait(0.2))
        return 0.0

    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=1000, clock=clock)
    open_breaker(breaker, 1)

    def probe():
        thread_role.probe = True
        with pytest.raises(CircuitOpenError):
            breaker.call(lambda: "never")

    def other():
        assert inside_critical_section.wait(THREAD_TIMEOUT)
        with pytest.raises(CircuitOpenError):
            breaker.call(lambda: "never")
        other_finished.set()

    probe_worker = threading.Thread(target=probe)
    other_worker = threading.Thread(target=other)
    other_worker.start()
    probe_worker.start()
    probe_worker.join(THREAD_TIMEOUT)
    other_worker.join(THREAD_TIMEOUT)

    assert not probe_worker.is_alive() and not other_worker.is_alive()
    assert observations == [False]


# Edge cases: qualifying failures and ignored exceptions ---------------------


def test_ignored_exceptions_neither_count_nor_reset_consecutive_failures():
    breaker = CircuitBreaker(failure_threshold=2, failure_on=(ConnectionError,))

    record_failure(breaker)
    with pytest.raises(ValueError):
        breaker.call(lambda: fail(ValueError("ignored")))
    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 1

    record_failure(breaker)
    assert breaker.state is CircuitState.OPEN


def test_breaker_opens_only_when_threshold_is_reached_after_a_reset_by_success():
    breaker = CircuitBreaker(failure_threshold=3)
    record_failure(breaker)
    record_failure(breaker)
    breaker.call(lambda: "ok")
    record_failure(breaker)
    record_failure(breaker)
    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 2
    record_failure(breaker)
    assert breaker.state is CircuitState.OPEN


def test_ignored_exception_propagates_as_the_same_object():
    breaker = CircuitBreaker(failure_on=(ConnectionError,))
    error = ValueError("business error")

    with pytest.raises(ValueError) as excinfo:
        breaker.call(lambda: fail(error))

    assert excinfo.value is error
    assert breaker.failure_count == 0


def test_ignored_exception_does_not_count_toward_success_threshold():
    clock = FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1,
        recovery_timeout=1,
        success_threshold=2,
        failure_on=(ConnectionError,),
        clock=clock,
    )
    open_breaker(breaker, 1)
    clock.advance(1)

    breaker.call(lambda: "one")
    with pytest.raises(ValueError):
        breaker.call(lambda: fail(ValueError("ignored")))
    assert breaker.state is CircuitState.HALF_OPEN
    breaker.call(lambda: "two")
    assert breaker.state is CircuitState.CLOSED


# Edge cases: stale results across state changes ------------------------------


def test_stale_failure_from_closed_epoch_does_not_reopen_half_open_circuit():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=1, clock=clock)

    def slow_failure():
        # Admitted while CLOSED; meanwhile another call opens the breaker and
        # the recovery timeout elapses.
        record_failure(breaker)
        clock.advance(1)
        assert breaker.state is CircuitState.HALF_OPEN
        raise ConnectionError("stale failure")

    with pytest.raises(ConnectionError, match="stale"):
        breaker.call(slow_failure)

    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.failure_count == 0


def test_stale_trial_failure_after_reset_is_discarded():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=1, clock=clock)
    open_breaker(breaker, 1)
    clock.advance(1)
    assert breaker.state is CircuitState.HALF_OPEN

    def trial_then_reset():
        breaker.reset()
        raise ConnectionError("stale trial failure")

    with pytest.raises(ConnectionError):
        breaker.call(trial_then_reset)

    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 0


def test_stale_trial_failure_after_circuit_closed_is_discarded():
    """Documented contract: results admitted before a state change are dropped."""
    clock = FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=3, recovery_timeout=1, half_open_max_calls=2, clock=clock
    )
    open_breaker(breaker, 3)
    clock.advance(1)

    def late_trial():
        breaker.call(lambda: "other trial closes the circuit")
        assert breaker.state is CircuitState.CLOSED
        raise ConnectionError("late trial failure")

    with pytest.raises(ConnectionError):
        breaker.call(late_trial)

    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 0


def test_half_open_permits_are_fully_restored_in_a_new_epoch():
    """A trial outliving HALF_OPEN -> OPEN -> HALF_OPEN must not skew permits."""
    clock = FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout=1, half_open_max_calls=2, clock=clock
    )
    open_breaker(breaker, 1)
    clock.advance(1)

    def outliving_trial():
        record_failure(breaker)  # second permit; its failure re-opens the circuit
        assert breaker.state is CircuitState.OPEN
        clock.advance(1)
        assert breaker.state is CircuitState.HALF_OPEN  # fresh epoch
        return "stale success"

    assert breaker.call(outliving_trial) == "stale success"
    assert breaker.state is CircuitState.HALF_OPEN

    # Exactly two permits must be available now: not one (leaked permit) and
    # not three (the stale release must not decrement the new epoch's count).
    admissions = []

    def outer():
        admissions.append("outer")

        def inner():
            admissions.append("inner")
            with pytest.raises(CircuitOpenError):
                breaker.call(lambda: admissions.append("third"))
            return "inner"

        breaker.call(inner)
        return "outer"

    breaker.call(outer)
    assert admissions == ["outer", "inner"]


# Edge cases: concurrency -----------------------------------------------------


def test_concurrent_failures_below_threshold_never_open_the_breaker():
    threshold = 8
    breaker = CircuitBreaker(failure_threshold=threshold, recovery_timeout=1000)
    workers = threshold - 1
    start = threading.Barrier(workers)

    def worker():
        start.wait(THREAD_TIMEOUT)
        with pytest.raises(ConnectionError):
            breaker.call(lambda: fail(ConnectionError("down")))

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(THREAD_TIMEOUT)

    assert all(not thread.is_alive() for thread in threads)
    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == workers

    # Concurrent successes reset the count and keep the circuit closed.
    start = threading.Barrier(16)

    def success_worker():
        start.wait(THREAD_TIMEOUT)
        assert breaker.call(lambda: "ok") == "ok"

    threads = [threading.Thread(target=success_worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(THREAD_TIMEOUT)

    assert all(not thread.is_alive() for thread in threads)
    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 0


def test_late_trial_success_after_reopen_is_discarded_across_threads():
    clock = FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout=1, half_open_max_calls=2, clock=clock
    )
    open_breaker(breaker, 1)
    clock.advance(1)
    assert breaker.state is CircuitState.HALF_OPEN

    started = threading.Event()
    release = threading.Event()
    results = {}

    def slow_trial():
        def blocking_operation():
            started.set()
            assert release.wait(THREAD_TIMEOUT)
            return "late success"

        results["value"] = breaker.call(blocking_operation)

    worker = threading.Thread(target=slow_trial)
    worker.start()
    try:
        assert started.wait(THREAD_TIMEOUT)
        # The second permit is used by a failing trial, which re-opens the circuit.
        record_failure(breaker)
        assert breaker.state is CircuitState.OPEN
    finally:
        release.set()
        worker.join(THREAD_TIMEOUT)

    assert not worker.is_alive()
    assert results["value"] == "late success"
    # The late success belonged to the previous HALF_OPEN epoch.
    assert breaker.state is CircuitState.OPEN
    clock.advance(1)
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.state is CircuitState.CLOSED


def test_outcome_recording_is_mutually_exclusive():
    """Another thread's admission check blocks while an outcome is being recorded.

    The clock is consulted inside the locked transition to OPEN, so the
    callback can observe whether a concurrent admission check gets through.
    """
    inside_critical_section = threading.Event()
    other_finished = threading.Event()
    observations = []
    thread_role = threading.local()

    def clock():
        if getattr(thread_role, "probe", False) and not inside_critical_section.is_set():
            inside_critical_section.set()
            observations.append(other_finished.wait(0.2))
        return 0.0

    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=1000, clock=clock)

    def probe():
        thread_role.probe = True
        # This failure reaches the threshold and records the OPEN transition.
        with pytest.raises(ConnectionError):
            breaker.call(lambda: fail(ConnectionError("down")))

    def other():
        assert inside_critical_section.wait(THREAD_TIMEOUT)
        with pytest.raises(CircuitOpenError):
            breaker.call(lambda: "never")
        other_finished.set()

    probe_worker = threading.Thread(target=probe)
    other_worker = threading.Thread(target=other)
    other_worker.start()
    probe_worker.start()
    probe_worker.join(THREAD_TIMEOUT)
    other_worker.join(THREAD_TIMEOUT)

    assert not probe_worker.is_alive() and not other_worker.is_alive()
    assert observations == [False]
    assert breaker.state is CircuitState.OPEN
