# API Reliability Toolkit

A small, dependency-light Python library for retrying transient failures in synchronous operations and for protecting failing dependencies with a circuit breaker.

## Features

- Configurable maximum attempts (including the initial call)
- Retry filtering by exception type
- Exponential backoff with an optional maximum delay
- Optional jitter
- Injectable sleep and randomness functions for deterministic testing
- The final retryable exception is re-raised when attempts are exhausted
- Thread-safe circuit breaker with `CLOSED`, `OPEN`, and `HALF_OPEN` states
- Optional circuit breaker integration for `retry_call`

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

### Circuit breaker

```python
from api_reliability import CircuitBreaker, CircuitOpenError, retry_call

breaker = CircuitBreaker(
    failure_threshold=5,      # consecutive failures before opening
    recovery_timeout=30.0,    # seconds to wait before allowing trial calls
    half_open_max_calls=1,    # concurrent trial calls allowed while HALF_OPEN
    failure_on=(ConnectionError, TimeoutError),
)

def call_remote_api():
    ...

try:
    result = retry_call(
        call_remote_api,
        max_attempts=3,
        retry_on=(ConnectionError, TimeoutError),
        circuit_breaker=breaker,
    )
except CircuitOpenError:
    # The dependency is considered down; fail fast or serve a fallback.
    ...
```

A breaker can also be used on its own with `breaker.call(operation)`. Share one
`CircuitBreaker` instance per protected dependency across threads.

## API

### `retry_call`

`retry_call(operation, *, max_attempts=3, retry_on=(Exception,), base_delay=0.1, max_delay=10.0, jitter=False, sleep=time.sleep, random_fn=random.random, circuit_breaker=None)`

Calls `operation` until it succeeds or a non-retryable exception occurs.

- `operation`: zero-argument callable.
- `max_attempts`: total number of calls; must be at least 1.
- `retry_on`: tuple of exception classes that may be retried. Defaults to `Exception`; production applications should consider narrowing this.
- `base_delay`: base delay in seconds before the first retry; must be non-negative.
- `max_delay`: cap for calculated delay; must be non-negative and at least `base_delay`.
- `jitter`: if enabled, multiplies delay by a random factor in `[0, 1)`.
- `sleep`: injectable waiting function, useful for tests.
- `random_fn`: injectable random source returning a number in `[0, 1)`.
- `circuit_breaker`: optional `CircuitBreaker`. When supplied, every attempt is routed through `circuit_breaker.call(operation)`.

The delay before retry number `n` (starting at 1) is `min(base_delay * 2 ** (n - 1), max_delay)`. With jitter enabled, that delay is multiplied by `random_fn()`.

Non-retryable exceptions propagate immediately. The final retryable exception is re-raised when attempts are exhausted.

### `CircuitBreaker`

`CircuitBreaker(*, failure_threshold=5, recovery_timeout=30.0, half_open_max_calls=1, success_threshold=1, failure_on=(Exception,), clock=time.monotonic)`

- `failure_threshold`: consecutive failures while `CLOSED` that open the circuit; must be at least 1.
- `recovery_timeout`: seconds the circuit stays `OPEN` before trial calls are admitted; must be non-negative.
- `half_open_max_calls`: maximum number of *concurrent* trial calls admitted while `HALF_OPEN`; must be at least 1.
- `success_threshold`: consecutive successful trial calls required to close the circuit; must be at least 1.
- `failure_on`: tuple of exception classes that count as failures. Any other exception is re-raised but does not affect the breaker.
- `clock`: injectable monotonic time source, useful for tests.

Members:

- `breaker.call(operation)`: runs the zero-argument `operation` if the breaker admits the call, otherwise raises `CircuitOpenError` without calling it. Exceptions from `operation` propagate unchanged.
- `breaker.state`: the current `CircuitState` (`CLOSED`, `OPEN`, or `HALF_OPEN`).
- `breaker.failure_count`: consecutive failures recorded while `CLOSED`; retained while `OPEN`.
- `breaker.reset()`: forces the breaker back to `CLOSED` and clears its counters.

Invalid configuration raises `CircuitBreakerConfigurationError` (a `ValueError`). `CircuitOpenError` exposes the rejecting breaker as `error.breaker`.

#### State transitions

| From        | To          | When                                                                 |
|-------------|-------------|----------------------------------------------------------------------|
| `CLOSED`    | `OPEN`      | `failure_threshold` consecutive failures (a success resets the count) |
| `OPEN`      | `HALF_OPEN` | `recovery_timeout` seconds have elapsed since opening                  |
| `HALF_OPEN` | `OPEN`      | any trial call fails (the timeout restarts)                            |
| `HALF_OPEN` | `CLOSED`    | `success_threshold` consecutive trial calls succeed                    |
| any         | `CLOSED`    | `breaker.reset()`                                                      |

While `OPEN`, calls are rejected immediately. While `HALF_OPEN`, at most `half_open_max_calls` calls may be in flight at once; additional calls are rejected until a permit is released. The `OPEN` to `HALF_OPEN` transition is evaluated lazily whenever a call is admitted or `state` / `failure_count` is read; no background thread is used.

#### Thread safety

All state transitions and `HALF_OPEN` admission decisions happen under a single lock; the protected operation itself runs outside the lock. Results of calls that were admitted before a state change are discarded, so a slow call cannot corrupt a newer state (for example, a stale success cannot close a circuit that re-opened in the meantime).

#### Interaction with retries

When `retry_call` is given a `circuit_breaker`:

- Each attempt is a separate `breaker.call`, so **every failed attempt counts as one breaker failure**. With `max_attempts=3` and `failure_threshold=5`, two exhausted retry calls are enough to open the circuit.
- `retry_on` decides what `retry_call` retries; `failure_on` decides what the breaker counts. They are independent: an exception can be retried without counting as a breaker failure, and vice versa.
- A `CircuitOpenError` raised by the supplied breaker propagates immediately. It does not consume further attempts and no delay is slept, even if `retry_on` would match it (for example with the default `retry_on=(Exception,)`).
- A `CircuitOpenError` raised from inside the operation by a *different* breaker is treated like any other exception: `retry_on` decides whether it is retried and `failure_on` decides whether it counts.

Without a `circuit_breaker`, `retry_call` behaves exactly as in the previous release.

## Development

```bash
python -m pip install -e ".[dev]"
pytest -v
ruff check .
```

## Scope and limitations

This milestone provides synchronous retry and circuit breaker behavior only. It does not implement HTTP clients, timeout enforcement, asynchronous operation, metrics, or distributed coordination. The circuit breaker's state is local to the process and to each `CircuitBreaker` instance; it counts consecutive failures rather than a failure rate over a sliding window, and it does not enforce timeouts on the protected operation. Retrying is safe only when the operation's semantics allow it; retrying a non-idempotent payment request can cause duplicate effects unless the remote API supports idempotency keys.
