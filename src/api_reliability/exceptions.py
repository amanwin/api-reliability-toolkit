"""Exceptions raised by the API Reliability Toolkit."""


class RetryConfigurationError(ValueError):
    """Raised when retry configuration is invalid."""


class CircuitBreakerConfigurationError(ValueError):
    """Raised when circuit breaker configuration is invalid."""


class CircuitOpenError(Exception):
    """Raised when a circuit breaker rejects a call without executing it.

    ``breaker`` references the :class:`~api_reliability.CircuitBreaker` that
    rejected the call, which lets callers distinguish rejections by a
    specific breaker from exceptions raised by the operation itself.
    """

    def __init__(self, message: str = "circuit breaker is open", *, breaker=None) -> None:
        super().__init__(message)
        self.breaker = breaker
