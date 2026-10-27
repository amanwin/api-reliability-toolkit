from api_reliability import RetryConfigurationError


def test_configuration_error_is_a_value_error():
    assert issubclass(RetryConfigurationError, ValueError)
