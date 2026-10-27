# API Reliability Toolkit

A small, dependency-light Python library for retrying transient failures in synchronous operations.

## Features

- Configurable maximum attempts (including the initial call)
- Retry filtering by exception type
- Exponential backoff with an optional maximum delay
- Optional jitter
- Injectable sleep, clock, and randomness functions for deterministic testing
- Optional overall timeout budget for deciding whether another retry may start
- Thread-safe circuit breaker for failing dependencies
- The final retryable exception is re-raised unchanged when attempts or the deadline stop retries

## Requirements

- Python 3.10+
- No runtime dependencies

## Installation

```bash
python -m pip install -e ".[dev]"
```

## Quick start

```python
from api_reliability import retry_call

attempts = 0

def flaky_operation():
    global attempts
    attempts += 1
    if attempts < 3:
        raise ConnectionError("temporary failure")
    return "success"

result = retry_call(
    flaky_operation,
    max_attempts=3,
    retry_on=(ConnectionError,),
    base_delay=0.1,
    max_delay=1.0,
)
assert result == "success"
```

`max_attempts` includes the initial call.

## Deadline-aware retries

Pass `timeout` to set a total time budget, in seconds, for the retry operation:

```python
from api_reliability import retry_call

result = retry_call(
    fetch_data,
    retry_on=(ConnectionError, TimeoutError),
    max_attempts=5,
    base_delay=0.25,
    max_delay=2.0,
    timeout=5.0,
)
```

The budget includes operation execution, circuit-breaker admission, and retry sleeps. The first attempt always runs, even when `timeout=0`; after a retryable failure, the next attempt is scheduled only if the full backoff delay would end strictly before the deadline. If the deadline prevents another retry, the last retryable exception is re-raised unchanged, just as it is when attempts are exhausted.

The default clock is `time.monotonic`. Tests can inject a clock and a matching sleep function. Both should use the same time source:

```python
class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

clock = FakeClock()
retry_call(operation, timeout=3, clock=clock, sleep=clock.sleep)
```

`timeout=None` preserves the prior retry behavior and does not call the clock. Timeout values must be finite, non-negative numbers; booleans are not accepted.

**Limitation:** This is a retry deadline, not hard cancellation. A synchronous operation that is already running can continue past the deadline. The timeout controls whether another retry is started; it does not forcibly interrupt arbitrary Python code or bound an individual network call. Use the relevant client's own timeout settings when a blocking operation must be bounded.

## API

`retry_call(operation, *, max_attempts=3, retry_on=(Exception,), base_delay=0.1, max_delay=10.0, jitter=False, sleep=time.sleep, random_fn=random.random, circuit_breaker=None, timeout=None, clock=time.monotonic)`

- `operation`: zero-argument callable.
- `max_attempts`: total number of calls; must be at least 1.
- `retry_on`: tuple of exception classes that may be retried. Defaults to `Exception`; production applications should consider narrowing this.
- `base_delay`: base delay in seconds before the first retry; must be non-negative.
- `max_delay`: cap for calculated delay; must be non-negative and at least `base_delay`.
- `jitter`: if enabled, multiplies delay by a random factor in `[0, 1)`.
- `sleep`: injectable waiting function, useful for tests.
- `random_fn`: injectable random source returning a number in `[0, 1)`.
- `circuit_breaker`: optional `CircuitBreaker`; each admitted failed attempt is counted by the breaker.
- `timeout`: optional total retry budget in seconds, or `None` for no deadline.
- `clock`: callable returning monotonic seconds. It is validated on every call but only invoked when `timeout` is configured.

The delay before retry number `n` (starting at 1) is `min(base_delay * 2 ** (n - 1), max_delay)`. With jitter enabled, that delay is multiplied by `random_fn()`.

Non-retryable exceptions propagate immediately. A rejection from the supplied circuit breaker also propagates immediately without another retry or sleep. The final retryable exception is re-raised unchanged when attempts are exhausted or the deadline prevents another retry.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -v
ruff check .
```

## Scope and limitations

The toolkit currently provides synchronous retries, deadline-aware retry scheduling, exponential backoff, and a thread-safe circuit breaker. It does not forcibly interrupt synchronous operations, provide HTTP-client wrappers, or implement asynchronous retries, metrics, or distributed coordination. Retrying is safe only when the operation's semantics allow it; retrying a non-idempotent payment request can cause duplicate effects unless the remote API supports idempotency keys.
