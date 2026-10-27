"""Backoff delay calculation helpers."""

import math

from .exceptions import RetryConfigurationError


def exponential_backoff(
    retry_number: int,
    *,
    base_delay: float = 0.1,
    max_delay: float = 10.0,
) -> float:
    """Return a capped exponential delay for a retry number starting at 1."""
    if isinstance(retry_number, bool) or not isinstance(retry_number, int):
        raise RetryConfigurationError("retry_number must be an integer")
    if retry_number < 1:
        raise RetryConfigurationError("retry_number must be at least 1")
    if math.isnan(base_delay) or base_delay < 0:
        raise RetryConfigurationError("base_delay must be non-negative")
    if math.isnan(max_delay) or max_delay < 0:
        raise RetryConfigurationError("max_delay must be non-negative")
    if max_delay < base_delay:
        raise RetryConfigurationError(
            "max_delay must be greater than or equal to base_delay"
        )

    delay = base_delay
    for _ in range(retry_number - 1):
        if delay >= max_delay:
            return max_delay
        delay *= 2
    return min(delay, max_delay)
