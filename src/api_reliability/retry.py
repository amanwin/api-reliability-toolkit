"""Synchronous retry execution with optional deadline budgeting."""

import math
import random
import time
from collections.abc import Callable
from typing import TypeVar

from .backoff import exponential_backoff
from .circuit_breaker import CircuitBreaker
from .exceptions import CircuitOpenError, RetryConfigurationError

T = TypeVar("T")


def retry_call(
    operation: Callable[[], T],
    *,
    max_attempts: int = 3,
    retry_on: tuple[type[Exception], ...] = (Exception,),
    base_delay: float = 0.1,
    max_delay: float = 10.0,
    jitter: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    random_fn: Callable[[], float] = random.random,
    circuit_breaker: CircuitBreaker | None = None,
    timeout: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Execute an operation, retrying selected exceptions with backoff.

    ``max_attempts`` includes the initial call. If all attempts are exhausted,
    the final retryable exception is re-raised unchanged.

    ``timeout`` is an optional total retry budget in seconds. It includes time
    spent in operations, breaker admission, and retry sleeps. The first attempt
    always runs, including when ``timeout=0``. After a retryable failure, a
    retry is scheduled only when its full backoff delay would end strictly
    before the deadline. If the deadline prevents a retry, the last exception
    is re-raised unchanged. This is a retry deadline, not a mechanism for
    interrupting an operation already in progress.

    ``clock`` must be a cheap, non-blocking, non-raising, non-decreasing
    monotonic time source. It is validated on every call but only invoked when
    ``timeout`` is not ``None``.

    When ``circuit_breaker`` is supplied, each admitted attempt goes through
    the breaker. Rejection by that supplied breaker propagates immediately,
    without another retry or sleep.
    """
    if not callable(operation):
        raise RetryConfigurationError("operation must be callable")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise RetryConfigurationError("max_attempts must be an integer")
    if max_attempts < 1:
        raise RetryConfigurationError("max_attempts must be at least 1")
    if not isinstance(retry_on, tuple) or not retry_on:
        raise RetryConfigurationError(
            "retry_on must be a non-empty tuple of exception classes"
        )
    if any(not isinstance(exc, type) or not issubclass(exc, Exception) for exc in retry_on):
        raise RetryConfigurationError("retry_on entries must be Exception subclasses")
    if not callable(sleep):
        raise RetryConfigurationError("sleep must be callable")
    if not callable(random_fn):
        raise RetryConfigurationError("random_fn must be callable")
    if not isinstance(jitter, bool):
        raise RetryConfigurationError("jitter must be a boolean")
    if circuit_breaker is not None and not isinstance(circuit_breaker, CircuitBreaker):
        raise RetryConfigurationError("circuit_breaker must be a CircuitBreaker or None")
    if not callable(clock):
        raise RetryConfigurationError("clock must be callable")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise RetryConfigurationError(
                "timeout must be None or a finite non-negative number"
            )
        try:
            timeout_is_finite = math.isfinite(timeout)
        except OverflowError:
            timeout_is_finite = False
        if not timeout_is_finite or timeout < 0:
            raise RetryConfigurationError(
                "timeout must be None or a finite non-negative number"
            )

    # Validate delays even when the operation succeeds immediately.
    exponential_backoff(1, base_delay=base_delay, max_delay=max_delay)

    deadline = None if timeout is None else clock() + float(timeout)

    for attempt in range(1, max_attempts + 1):
        try:
            if circuit_breaker is None:
                return operation()
            return circuit_breaker.call(operation)
        except retry_on as exc:
            if (
                circuit_breaker is not None
                and isinstance(exc, CircuitOpenError)
                and exc.breaker is circuit_breaker
            ):
                raise
            if attempt == max_attempts:
                raise

            delay = exponential_backoff(
                attempt,
                base_delay=base_delay,
                max_delay=max_delay,
            )
            if jitter:
                factor = random_fn()
                if (
                    not isinstance(factor, (int, float))
                    or isinstance(factor, bool)
                    or not math.isfinite(factor)
                    or not 0 <= factor < 1
                ):
                    raise RetryConfigurationError(
                        "random_fn must return a number in [0, 1)"
                    )
                delay *= factor

            # A full delay must fit strictly before the deadline. Do not clip
            # the sleep or re-check after it: a real sleep may overshoot.
            if deadline is not None and clock() + delay >= deadline:
                raise
            sleep(delay)

    raise RuntimeError("unreachable retry state")
