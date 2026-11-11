"""
Tests for opsmesh.retry — exponential backoff + jitter + Retry-After.

Covers:
  - Happy path: func succeeds on first call
  - Retries on TimeoutError, then succeeds
  - Retries on ConnectionError, then succeeds
  - Retries on TransientError (5xx wrapper), then succeeds
  - Retries on RateLimitError, honors Retry-After
  - Does NOT retry on ValueError (non-retriable)
  - RetryExhausted after max_attempts
  - Backoff delay computation with jitter
  - Custom exception types
"""

from __future__ import annotations

import asyncio

import pytest

from opsmesh.retry import (
    DEFAULT_RETRIABLE,
    RateLimitError,
    RetryExhausted,
    TransientError,
    compute_backoff_delay,
    retry_with_backoff,
)


# ---------------------------------------------------------------------------
# 1. compute_backoff_delay
# ---------------------------------------------------------------------------


class TestComputeBackoffDelay:
    def test_returns_positive_float(self):
        delay = compute_backoff_delay(attempt=0)
        assert isinstance(delay, float)
        assert delay > 0

    def test_exponential_growth(self):
        """Without jitter, delay roughly doubles each attempt."""
        # Run many times and check the base (exponential) part grows
        # Since jitter is random(0, base), the total is exponential + jitter
        # We check that later attempts have larger minimum delays
        d0_min = 1.0  # base * 2^0 = 1.0, jitter adds 0-1
        d1_min = 2.0  # base * 2^1 = 2.0
        d2_min = 4.0  # base * 2^2 = 4.0
        assert compute_backoff_delay(0) >= d0_min
        assert compute_backoff_delay(1) >= d1_min
        assert compute_backoff_delay(2) >= d2_min

    def test_max_delay_cap(self):
        """Delay is capped at max_delay + base_delay (jitter)."""
        delay = compute_backoff_delay(attempt=20, base_delay=1.0, max_delay=60.0)
        # exponential part is capped at 60, jitter is 0-1
        assert delay <= 61.0

    def test_custom_base_and_max(self):
        delay = compute_backoff_delay(attempt=0, base_delay=0.5, max_delay=10.0)
        assert 0.5 <= delay <= 1.0  # 0.5 exponential + 0-0.5 jitter


# ---------------------------------------------------------------------------
# 2. Happy path — succeeds on first try
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_call(self):
        call_count = 0

        async def good_func():
            nonlocal call_count
            call_count += 1
            return {"status": "ok"}

        result = await retry_with_backoff(good_func, max_attempts=4, base_delay=0.01)
        assert result == {"status": "ok"}
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_passes_args_and_kwargs(self):
        async def add_func(a, b, multiplier=1):
            return (a + b) * multiplier

        result = await retry_with_backoff(add_func, 3, 4, multiplier=2, max_attempts=2, base_delay=0.01)
        assert result == 14


# ---------------------------------------------------------------------------
# 3. Retries on transient errors, then succeeds
# ---------------------------------------------------------------------------


class TestRetryOnTransient:
    @pytest.mark.asyncio
    async def test_retries_on_timeout_then_succeeds(self):
        """A func that times out twice, then succeeds, should be retried."""
        call_count = 0

        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise TimeoutError("simulated timeout")
            return {"status": "ok"}

        result = await retry_with_backoff(flaky_func, max_attempts=4, base_delay=0.01)
        assert result == {"status": "ok"}
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retries_on_connection_error(self):
        call_count = 0

        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("network down")
            return "ok"

        result = await retry_with_backoff(flaky_func, max_attempts=3, base_delay=0.01)
        assert result == "ok"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self):
        """TransientError (5xx wrapper) is retriable."""
        call_count = 0

        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise TransientError("503 service unavailable")
            return "ok"

        result = await retry_with_backoff(flaky_func, max_attempts=3, base_delay=0.01)
        assert result == "ok"
        assert call_count == 2


# ---------------------------------------------------------------------------
# 4. RateLimitError honors Retry-After
# ---------------------------------------------------------------------------


class TestRetryAfter:
    @pytest.mark.asyncio
    async def test_rate_limit_error_is_retriable(self):
        call_count = 0

        async def rate_limited_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RateLimitError("429 too many requests", retry_after=0.01)
            return "ok"

        result = await retry_with_backoff(rate_limited_func, max_attempts=3, base_delay=0.01)
        assert result == "ok"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_after_honored(self):
        """When RateLimitError has retry_after, the sleep should be at least
        that long. We verify by tracking sleep calls."""
        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        call_count = 0

        async def func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RateLimitError("429", retry_after=5.0)
            return "ok"

        await retry_with_backoff(
            func, max_attempts=2, base_delay=0.01, sleep_func=fake_sleep
        )
        # The sleep should be >= 5.0 (retry_after)
        assert len(sleep_calls) == 1
        assert sleep_calls[0] >= 5.0

    @pytest.mark.asyncio
    async def test_retry_after_capped(self):
        """Retry-After is capped at retry_after_cap to prevent indefinite stalls."""
        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        call_count = 0

        async def func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RateLimitError("429", retry_after=99999.0)
            return "ok"

        await retry_with_backoff(
            func, max_attempts=2, base_delay=0.01,
            sleep_func=fake_sleep, retry_after_cap=10.0,
        )
        # The sleep should be capped at 10.0
        assert sleep_calls[0] <= 10.0


# ---------------------------------------------------------------------------
# 5. Non-retriable errors propagate immediately
# ---------------------------------------------------------------------------


class TestNonRetriable:
    @pytest.mark.asyncio
    async def test_value_error_not_retried(self):
        """ValueError is a programming bug, not a transient failure.
        It should propagate immediately on first occurrence."""
        call_count = 0

        async def bad_func():
            nonlocal call_count
            call_count += 1
            raise ValueError("bad input")

        with pytest.raises(ValueError):
            await retry_with_backoff(bad_func, max_attempts=4, base_delay=0.01)
        assert call_count == 1  # only called once — not retried

    @pytest.mark.asyncio
    async def test_type_error_not_retried(self):
        call_count = 0

        async def bad_func():
            nonlocal call_count
            call_count += 1
            raise TypeError("wrong type")

        with pytest.raises(TypeError):
            await retry_with_backoff(bad_func, max_attempts=4, base_delay=0.01)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_custom_exception_not_retried_by_default(self):
        """Custom exceptions are NOT retried unless included in the exceptions tuple."""
        call_count = 0

        class CustomError(Exception):
            pass

        async def func():
            nonlocal call_count
            call_count += 1
            raise CustomError("custom")

        with pytest.raises(CustomError):
            await retry_with_backoff(func, max_attempts=4, base_delay=0.01)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_custom_exception_retried_when_included(self):
        """Custom exceptions ARE retried when added to the exceptions tuple."""
        call_count = 0

        class CustomError(Exception):
            pass

        async def func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise CustomError("custom")
            return "ok"

        result = await retry_with_backoff(
            func, max_attempts=3, base_delay=0.01,
            exceptions=(CustomError,),
        )
        assert result == "ok"
        assert call_count == 2


# ---------------------------------------------------------------------------
# 6. RetryExhausted
# ---------------------------------------------------------------------------


class TestRetryExhausted:
    @pytest.mark.asyncio
    async def test_exhausted_after_max_attempts(self):
        """All attempts fail → RetryExhausted with the last exception."""
        call_count = 0

        async def always_fails():
            nonlocal call_count
            call_count += 1
            raise TimeoutError("always times out")

        with pytest.raises(RetryExhausted) as exc_info:
            await retry_with_backoff(always_fails, max_attempts=3, base_delay=0.01)

        assert call_count == 3
        assert exc_info.value.attempts == 3
        assert isinstance(exc_info.value.last_exception, TimeoutError)

    @pytest.mark.asyncio
    async def test_exhausted_preserves_last_exception(self):
        """The last exception is chained (available via __cause__)."""

        async def always_fails():
            raise ConnectionError("network down")

        with pytest.raises(RetryExhausted) as exc_info:
            await retry_with_backoff(always_fails, max_attempts=2, base_delay=0.01)

        assert isinstance(exc_info.value.last_exception, ConnectionError)


# ---------------------------------------------------------------------------
# 7. DEFAULT_RETRIABLE contents
# ---------------------------------------------------------------------------


class TestDefaultRetriable:
    def test_includes_timeout(self):
        assert TimeoutError in DEFAULT_RETRIABLE

    def test_includes_connection_error(self):
        assert ConnectionError in DEFAULT_RETRIABLE

    def test_includes_transient_error(self):
        assert TransientError in DEFAULT_RETRIABLE

    def test_includes_rate_limit_error(self):
        assert RateLimitError in DEFAULT_RETRIABLE

    def test_does_not_include_value_error(self):
        """ValueError is NOT retriable (programming bug)."""
        assert ValueError not in DEFAULT_RETRIABLE

    def test_does_not_include_type_error(self):
        assert TypeError not in DEFAULT_RETRIABLE
