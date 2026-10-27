# API Reliability Toolkit

A small, dependency-light Python library for retrying transient failures in synchronous operations.

## Features

- Configurable maximum attempts (including the initial call)
- Retry filtering by exception type
- Exponential backoff with an optional maximum delay
- Optional jitter
- Injectable sleep and randomness functions for deterministic testing
- The final retryable exception is re-raised when attempts are exhausted

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

## API

`retry_call(operation, *, max_attempts=3, retry_on=(Exception,), base_delay=0.1, max_delay=10.0, jitter=False, sleep=time.sleep, random_fn=random.random)`

Calls `operation` until it succeeds or a non-retryable exception occurs.

- `operation`: zero-argument callable.
- `max_attempts`: total number of calls; must be at least 1.
- `retry_on`: tuple of exception classes that may be retried. Defaults to `Exception`; production applications should consider narrowing this.
- `base_delay`: base delay in seconds before the first retry; must be non-negative.
- `max_delay`: cap for calculated delay; must be non-negative and at least `base_delay`.
- `jitter`: if enabled, multiplies delay by a random factor in `[0, 1)`.
- `sleep`: injectable waiting function, useful for tests.
- `random_fn`: injectable random source returning a number in `[0, 1)`.

The delay before retry number `n` (starting at 1) is `min(base_delay * 2 ** (n - 1), max_delay)`. With jitter enabled, that delay is multiplied by `random_fn()`.

Non-retryable exceptions propagate immediately. The final retryable exception is re-raised when attempts are exhausted.

## Development

```bash
python -m pip install -e ".[dev]"
pytest -v
ruff check .
```

## Scope and limitations

This milestone provides synchronous retry behavior only. It does not implement HTTP clients, timeout enforcement, circuit breakers, asynchronous retries, metrics, or distributed coordination. Retrying is safe only when the operation's semantics allow it; retrying a non-idempotent payment request can cause duplicate effects unless the remote API supports idempotency keys.
