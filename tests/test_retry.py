import pytest

from api_reliability import RetryConfigurationError, retry_call


def test_success_returns_result_without_sleeping():
    sleeps = []
    result = retry_call(lambda: "ok", sleep=sleeps.append)
    assert result == "ok"
    assert sleeps == []


def test_retries_then_returns_success():
    calls = []
    sleeps = []

    def operation():
        calls.append("call")
        if len(calls) < 3:
            raise ConnectionError("temporary")
        return 42

    result = retry_call(
        operation,
        max_attempts=3,
        retry_on=(ConnectionError,),
        base_delay=0.1,
        max_delay=1,
        sleep=sleeps.append,
    )

    assert result == 42
    assert len(calls) == 3
    assert sleeps == [0.1, 0.2]


def test_non_retryable_exception_propagates_immediately():
    calls = []

    def operation():
        calls.append("call")
        raise ValueError("not retryable")

    with pytest.raises(ValueError, match="not retryable"):
        retry_call(
            operation,
            max_attempts=5,
            retry_on=(ConnectionError,),
            sleep=lambda _: pytest.fail("sleep should not be called"),
        )

    assert len(calls) == 1


def test_final_retryable_exception_is_reraised():
    calls = []

    def operation():
        calls.append("call")
        raise ConnectionError("still down")

    with pytest.raises(ConnectionError, match="still down"):
        retry_call(
            operation,
            max_attempts=3,
            retry_on=(ConnectionError,),
            sleep=lambda _: None,
        )

    assert len(calls) == 3


def test_max_attempts_one_means_no_retry():
    calls = []

    def operation():
        calls.append("call")
        raise ConnectionError("failure")

    with pytest.raises(ConnectionError):
        retry_call(operation, max_attempts=1, retry_on=(ConnectionError,))

    assert len(calls) == 1


def test_jitter_scales_delay():
    calls = []
    sleeps = []

    def operation():
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("temporary")
        return "done"

    result = retry_call(
        operation,
        max_attempts=2,
        retry_on=(ConnectionError,),
        base_delay=2,
        max_delay=10,
        jitter=True,
        sleep=sleeps.append,
        random_fn=lambda: 0.25,
    )

    assert result == "done"
    assert sleeps == [0.5]


def test_invalid_configuration_is_rejected():
    with pytest.raises(RetryConfigurationError):
        retry_call(lambda: None, max_attempts=0)

    with pytest.raises(RetryConfigurationError):
        retry_call(lambda: None, retry_on=())

    with pytest.raises(RetryConfigurationError):
        retry_call(lambda: None, retry_on=(str,))


def test_invalid_jitter_value_is_rejected():
    def failing_operation():
        raise ConnectionError("temporary")

    with pytest.raises(RetryConfigurationError):
        retry_call(
            failing_operation,
            max_attempts=2,
            retry_on=(ConnectionError,),
            jitter=True,
            random_fn=lambda: 1.0,
            sleep=lambda _: None,
        )


@pytest.mark.parametrize(
    ("base_delay", "max_delay"),
    [(float("nan"), 1.0), (0.1, float("nan"))],
)
def test_nan_delays_are_rejected_before_the_operation_runs(base_delay, max_delay):
    """Regression: NaN delays used to fail late inside ``sleep`` after the first attempt."""
    calls = []

    with pytest.raises(RetryConfigurationError):
        retry_call(
            lambda: calls.append("call"),
            base_delay=base_delay,
            max_delay=max_delay,
            sleep=lambda _: pytest.fail("sleep should not be called"),
        )

    assert calls == []
