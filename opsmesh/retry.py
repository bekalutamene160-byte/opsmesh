"""
OPSMESH Retry with Backoff — exponential backoff + jitter + Retry-After.

Architecture (course Ch 8):
  External systems fail. APIs rate-limit, networks flake, LLM providers have
  outages. A reliable agent expects every external call to fail sometimes
  and treats success as a happy outcome — not the default.

THE RETRY FORMULA (AWS pattern — "exponential backoff with full jitter"):
  delay = min(base_delay * (2 ** attempt), max_delay) + random.uniform(0, base_delay)

  Why jitter?
    Without jitter: 100 requests fail at the same time. They all retry at
    1s, 2s, 4s, 8s — synchronized spikes that hammer the provider.
    With jitter: delays are spread out, preventing thundering herd.

WHAT TO RETRY (course Ch 8.2):
  - 200:           No (success)
  - 4xx (400,401,403,404,409,422): No (client error — same args = same failure)
  - 429:           Yes (rate limit — honor Retry-After header)
  - 5xx:           Yes (server error — often transient)
  - TimeoutError:  Yes (network or slow server)
  - ConnectionError: Yes (network failure — almost always transient)

WHAT NOT TO RETRY:
  - 4xx errors. Retrying a 401 (token expired) 4 times burns quota, then
    fails anyway. Fail fast and visibly.

HOW IT WORKS:
  1. Call func(*args, **kwargs).
  2. If it raises a retriable exception:
     a. Compute delay = min(base * 2^attempt, max) + random(0, base)
     b. If RateLimitError with retry_after, use max(delay, retry_after)
     c. Sleep for delay seconds
     d. Try again
  3. If max_attempts reached, raise RetryExhausted with the last error.
  4. If a NON-retriable exception is raised, propagate immediately.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Optional, Tuple, Type


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------


class RetryExhausted(Exception):
    """All retry attempts failed with retriable exceptions.

    The original exception is available as .last_exception."""

    def __init__(self, last_exception: Exception, attempts: int):
        self.last_exception = last_exception
        self.attempts = attempts
        super().__init__(
            f"Retry exhausted after {attempts} attempts. "
            f"Last error: {type(last_exception).__name__}: {last_exception}"
        )


class RateLimitError(Exception):
    """Raised when a provider returns 429 (Too Many Requests).

    Carries retry_after (seconds) if the provider included a Retry-After header.
    The retry function honors this value, using max(computed_backoff, retry_after).
    """

    def __init__(self, message: str, retry_after: Optional[float] = None):
        self.retry_after = retry_after
        super().__init__(message)


class TransientError(Exception):
    """Raised for 5xx server errors and other transient failures.

    This is the catch-all for provider-side issues that are likely to resolve
    on retry. The caller wraps provider 5xx responses in this exception."""


# Default exception types that should be retried.
# NOTE: this does NOT include ValueError, TypeError, KeyError, etc.
# Those are programming bugs, not transient failures.
DEFAULT_RETRIABLE: Tuple[Type[Exception], ...] = (
    TimeoutError,
    ConnectionError,
    TransientError,
    RateLimitError,
)


# ---------------------------------------------------------------------------
# Backoff computation
# ---------------------------------------------------------------------------


def compute_backoff_delay(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> float:
    """
    Compute the backoff delay with full jitter (AWS pattern).

    Formula:
        exponential = min(base_delay * (2 ** attempt), max_delay)
        jitter      = random.uniform(0, base_delay)
        delay       = exponential + jitter

    Args:
        attempt: 0-based attempt number (0 = first retry, 1 = second, ...).
        base_delay: base delay in seconds. Default 1.0.
        max_delay: maximum delay cap in seconds. Default 60.0.

    Returns:
        Delay in seconds (float, always > 0).
    """
    exponential = min(base_delay * (2 ** attempt), max_delay)
    jitter = random.uniform(0, base_delay)
    return exponential + jitter


# ---------------------------------------------------------------------------
# The retry function
# ---------------------------------------------------------------------------


async def retry_with_backoff(
    func: Callable[..., Awaitable[Any]],
    *args: Any,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exceptions: Tuple[Type[Exception], ...] = DEFAULT_RETRIABLE,
    retry_after_cap: float = 300.0,
    sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    **kwargs: Any,
) -> Any:
    """
    Retry an async callable with exponential backoff + jitter.

    Only retries the specified exception types. All other exceptions propagate
    immediately (fail fast on non-retriable errors).

    Args:
        func: async callable to retry.
        *args: positional args passed to func.
        max_attempts: total attempts including the first call. Default 4.
        base_delay: base delay in seconds. Default 1.0.
        max_delay: maximum delay cap in seconds. Default 60.0.
        exceptions: exception types to retry. Default: transient errors.
        retry_after_cap: max seconds to honor Retry-After. Default 300 (5 min).
        sleep_func: injectable sleep function (for tests). Default: asyncio.sleep.
        **kwargs: keyword args passed to func.

    Returns:
        The result of func(*args, **kwargs) on success.

    Raises:
        RetryExhausted: if all attempts fail with retriable exceptions.
        Any non-retriable exception: propagates immediately on first occurrence.
    """
    last_exception: Optional[Exception] = None

    for attempt in range(max_attempts):
        try:
            return await func(*args, **kwargs)
        except exceptions as e:
            last_exception = e

            # If this was the last attempt, don't sleep — raise
            if attempt + 1 >= max_attempts:
                raise RetryExhausted(e, max_attempts) from e

            # Compute the backoff delay
            delay = compute_backoff_delay(attempt, base_delay, max_delay)

            # Honor Retry-After if the exception carries it (RateLimitError)
            if isinstance(e, RateLimitError) and e.retry_after is not None:
                # Use the LARGER of computed backoff or retry_after
                # (providers know their own rate limits better than we do)
                # But cap at retry_after_cap to prevent indefinite stalls
                delay = max(delay, min(e.retry_after, retry_after_cap))

            await sleep_func(delay)

    # Shouldn't reach here, but just in case
    raise RetryExhausted(last_exception or RuntimeError("unknown"), max_attempts)


__all__ = [
    "RetryExhausted",
    "RateLimitError",
    "TransientError",
    "DEFAULT_RETRIABLE",
    "compute_backoff_delay",
    "retry_with_backoff",
]
