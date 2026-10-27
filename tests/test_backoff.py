import pytest

from api_reliability import RetryConfigurationError, exponential_backoff


def test_exponential_backoff_grows_exponentially():
    assert exponential_backoff(1, base_delay=0.25, max_delay=10) == 0.25
    assert exponential_backoff(2, base_delay=0.25, max_delay=10) == 0.5
    assert exponential_backoff(3, base_delay=0.25, max_delay=10) == 1.0


def test_exponential_backoff_is_capped():
    assert exponential_backoff(10, base_delay=1, max_delay=4) == 4


@pytest.mark.parametrize("retry_number", [0, -1, True, 1.5, "2"])
def test_invalid_retry_number_is_rejected(retry_number):
    with pytest.raises(RetryConfigurationError):
        exponential_backoff(retry_number)


@pytest.mark.parametrize(
    ("base_delay", "max_delay"),
    [(-1, 2), (1, -1), (2, 1), (float("nan"), 2), (1, float("nan"))],
)
def test_invalid_delays_are_rejected(base_delay, max_delay):
    with pytest.raises(RetryConfigurationError):
        exponential_backoff(1, base_delay=base_delay, max_delay=max_delay)
