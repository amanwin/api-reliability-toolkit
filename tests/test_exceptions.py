from api_reliability import (
    CircuitBreakerConfigurationError,
    CircuitOpenError,
    RetryConfigurationError,
)


def test_configuration_error_is_a_value_error():
    assert issubclass(RetryConfigurationError, ValueError)


def test_circuit_breaker_configuration_error_is_a_value_error():
    assert issubclass(CircuitBreakerConfigurationError, ValueError)


def test_circuit_open_error_defaults():
    error = CircuitOpenError()
    assert isinstance(error, Exception)
    assert not isinstance(error, ValueError)
    assert str(error) == "circuit breaker is open"
    assert error.breaker is None


def test_circuit_open_error_keeps_breaker_reference():
    sentinel = object()
    error = CircuitOpenError("custom message", breaker=sentinel)
    assert str(error) == "custom message"
    assert error.breaker is sentinel
