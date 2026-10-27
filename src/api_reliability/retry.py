"""Synchronous retry execution."""

import random
import time
from collections.abc import Callable
from typing import TypeVar

from .backoff import exponential_backoff
from .exceptions import RetryConfigurationError

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
) -> T:
    """Execute a zero-argument operation, retrying selected exceptions.

    ``max_attempts`` includes the initial call. The final retryable exception
    is re-raised unchanged when all attempts are exhausted.
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

    # Validate delays even when the operation succeeds immediately.
    exponential_backoff(1, base_delay=base_delay, max_delay=max_delay)

    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except retry_on:
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
