"""Thread-safe circuit breaker for synchronous operations."""

import math
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import TypeVar

from .exceptions import CircuitBreakerConfigurationError, CircuitOpenError

T = TypeVar("T")


class CircuitState(Enum):
    """Observable states of a :class:`CircuitBreaker`."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class _Outcome(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    IGNORED = "ignored"


class CircuitBreaker:
    """Stop calling a failing dependency until it has had time to recover.

    State transitions:

    * ``CLOSED`` -> ``OPEN`` once ``failure_threshold`` consecutive failures
      are recorded. A success resets the failure count.
    * ``OPEN`` -> ``HALF_OPEN`` once ``recovery_timeout`` seconds have elapsed.
      While ``OPEN``, calls are rejected with :class:`CircuitOpenError`.
    * ``HALF_OPEN`` admits at most ``half_open_max_calls`` concurrent trial
      calls; additional calls are rejected. Any trial failure returns the
      breaker to ``OPEN`` and restarts the timeout. ``success_threshold``
      consecutive trial successes return it to ``CLOSED``.

    Only exceptions matching ``failure_on`` count as failures. Any other
    exception (including ``BaseException`` subclasses such as
    ``KeyboardInterrupt``) is ignored by the breaker: it neither counts as a
    failure nor as a success, but it always releases a ``HALF_OPEN`` trial
    permit. Ignored exceptions are re-raised unchanged.

    The ``OPEN`` -> ``HALF_OPEN`` transition is evaluated lazily whenever a
    call is admitted or :attr:`state` is read; no background thread is used.
    All state changes are guarded by a lock, and the operation itself runs
    outside of it.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
        success_threshold: int = 1,
        failure_on: tuple[type[Exception], ...] = (Exception,),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _validate_positive_int("failure_threshold", failure_threshold)
        _validate_positive_int("half_open_max_calls", half_open_max_calls)
        _validate_positive_int("success_threshold", success_threshold)
        if (
            isinstance(recovery_timeout, bool)
            or not isinstance(recovery_timeout, (int, float))
            or math.isnan(recovery_timeout)
            or recovery_timeout < 0
        ):
            raise CircuitBreakerConfigurationError("recovery_timeout must be a non-negative number")
        if not isinstance(failure_on, tuple) or not failure_on:
            raise CircuitBreakerConfigurationError(
                "failure_on must be a non-empty tuple of exception classes"
            )
        if any(not isinstance(exc, type) or not issubclass(exc, Exception) for exc in failure_on):
            raise CircuitBreakerConfigurationError(
                "failure_on entries must be Exception subclasses"
            )
        if not callable(clock):
            raise CircuitBreakerConfigurationError("clock must be callable")

        self._failure_threshold = failure_threshold
        self._recovery_timeout = float(recovery_timeout)
        self._half_open_max_calls = half_open_max_calls
        self._success_threshold = success_threshold
        self._failure_on = failure_on
        self._clock = clock

        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._half_open_successes = 0
        self._half_open_in_flight = 0
        self._opened_at = 0.0
        # Incremented on every state transition. Outcomes of calls admitted in
        # an earlier generation are stale and must not influence the new state.
        self._generation = 0

    # Public interface ---------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """Current state, applying the timed ``OPEN`` -> ``HALF_OPEN`` transition."""
        with self._lock:
            self._refresh_state_locked()
            return self._state

    @property
    def failure_count(self) -> int:
        """Consecutive failures recorded while ``CLOSED``.

        The count is retained while ``OPEN`` and cleared on entering
        ``HALF_OPEN`` or ``CLOSED``. Like :attr:`state`, reading it applies
        the timed ``OPEN`` -> ``HALF_OPEN`` transition.
        """
        with self._lock:
            self._refresh_state_locked()
            return self._failure_count

    def call(self, operation: Callable[[], T]) -> T:
        """Execute ``operation`` if the breaker admits the call.

        Raises :class:`CircuitOpenError` without calling ``operation`` when
        the breaker is ``OPEN`` or the ``HALF_OPEN`` trial limit is reached.
        Exceptions raised by ``operation`` propagate unchanged.
        """
        if not callable(operation):
            raise CircuitBreakerConfigurationError("operation must be callable")

        generation = self._acquire()
        try:
            result = operation()
        except BaseException as exc:
            outcome = _Outcome.FAILURE if isinstance(exc, self._failure_on) else _Outcome.IGNORED
            self._release(generation, outcome)
            raise
        self._release(generation, _Outcome.SUCCESS)
        return result

    def reset(self) -> None:
        """Force the breaker back to ``CLOSED`` and clear all counters."""
        with self._lock:
            self._transition_locked(CircuitState.CLOSED)

    # Internal state machine --------------------------------------------

    def _acquire(self) -> int:
        """Admit a call or raise :class:`CircuitOpenError`; return the generation."""
        with self._lock:
            self._refresh_state_locked()
            if self._state is CircuitState.OPEN:
                raise CircuitOpenError("circuit breaker is open", breaker=self)
            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_in_flight >= self._half_open_max_calls:
                    raise CircuitOpenError(
                        "circuit breaker is half-open and its trial call limit is reached",
                        breaker=self,
                    )
                self._half_open_in_flight += 1
            return self._generation

    def _release(self, generation: int, outcome: _Outcome) -> None:
        """Record the outcome of a call admitted in ``generation``."""
        with self._lock:
            if generation != self._generation:
                # The breaker changed state while this call was in flight; the
                # result belongs to a previous epoch and is discarded.
                return

            if self._state is CircuitState.HALF_OPEN:
                self._half_open_in_flight -= 1

            if outcome is _Outcome.IGNORED:
                return

            failed = outcome is _Outcome.FAILURE
            if self._state is CircuitState.CLOSED:
                if failed:
                    self._failure_count += 1
                    if self._failure_count >= self._failure_threshold:
                        self._transition_locked(CircuitState.OPEN)
                else:
                    self._failure_count = 0
            elif self._state is CircuitState.HALF_OPEN:
                if failed:
                    self._transition_locked(CircuitState.OPEN)
                else:
                    self._half_open_successes += 1
                    if self._half_open_successes >= self._success_threshold:
                        self._transition_locked(CircuitState.CLOSED)

    def _refresh_state_locked(self) -> None:
        """Apply the timed ``OPEN`` -> ``HALF_OPEN`` transition. Caller holds the lock."""
        if (
            self._state is CircuitState.OPEN
            and self._clock() - self._opened_at >= self._recovery_timeout
        ):
            self._transition_locked(CircuitState.HALF_OPEN)

    def _transition_locked(self, new_state: CircuitState) -> None:
        """Move to ``new_state`` and reset per-state counters. Caller holds the lock."""
        self._generation += 1
        self._state = new_state
        self._half_open_successes = 0
        self._half_open_in_flight = 0
        if new_state is CircuitState.OPEN:
            # Keep the failure count visible while OPEN for observability.
            self._opened_at = self._clock()
        else:
            self._failure_count = 0


def _validate_positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CircuitBreakerConfigurationError(f"{name} must be an integer")
    if value < 1:
        raise CircuitBreakerConfigurationError(f"{name} must be at least 1")
