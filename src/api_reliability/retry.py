"""Synchronous retry execution."""

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
) -> T:
    """Execute a zero-argument operation, retrying selected exceptions.

    ``max_attempts`` includes the initial call. The final retryable exception
    is re-raised unchanged when all attempts are exhausted.

    When ``circuit_breaker`` is supplied, every attempt is routed through
    :meth:`CircuitBreaker.call`, so each failed attempt counts as one breaker
    failure. A :class:`CircuitOpenError` raised by that breaker propagates
    immediately: no further attempts are made and no delay is slept, even if
    ``retry_on`` would otherwise match it.
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

    # Validate delays even when the operation succeeds immediately.
    exponential_backoff(1, base_delay=base_delay, max_delay=max_delay)

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
                # Rejected by the supplied breaker: never retry.
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
                    or not 0 <= factor < 1
                ):
                    raise RetryConfigurationError(
                        "random_fn must return a number in [0, 1)"
                    )
                delay *= factor
            sleep(delay)

    raise RuntimeError("unreachable retry state")
