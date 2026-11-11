"""
Failure-injection tests for OPSMESH reliability primitives.

These tests deliberately INJECT failures into mock providers to verify that
the system recovers correctly. They cover the 8 failure cases from the
master course (Ch 19) plus the Day 3 reliability primitives.

Test categories:
  1. Retry behavior — timeout, 5xx, 429 (with Retry-After), 4xx (no retry)
  2. Idempotency — duplicate sends prevented, crash recovery
  3. Budget enforcement — tokens, cost, time, tool calls
  4. Combined — retry + idempotency (the key reliability pattern)
"""

from __future__ import annotations

import asyncio

import pytest

from opsmesh.budget import BudgetExceeded, RunBudget
from opsmesh.idempotency import IdempotentToolExecutor, InProgressError, make_idempotency_key
from opsmesh.models import IdempotencyRecord, IdempotencyStatus
from opsmesh.retry import (
    RateLimitError,
    RetryExhausted,
    TransientError,
    retry_with_backoff,
)
from opsmesh.state_store import InMemoryStateStore


# ---------------------------------------------------------------------------
# 1. Failure Case 1: Agent loops forever (budget prevents this)
# ---------------------------------------------------------------------------


class TestBudgetPreventsInfiniteLoop:
    def test_tool_call_budget_stops_loop(self):
        """A run that calls tools in a loop is stopped by the tool_call budget.

        Course Case 1: 'Agent loops forever — consumes 12,000 tokens in 3 min.'
        Fix: RunBudget.max_tool_calls + BudgetExceeded."""
        budget = RunBudget(max_tool_calls=3)
        call_count = 0

        while True:
            try:
                budget.tool_call()
                call_count += 1
            except BudgetExceeded:
                break

        assert call_count == 3  # stopped after 3 (the max)

    def test_token_budget_stops_loop(self):
        """A run that burns tokens in a loop is stopped by the token budget."""
        budget = RunBudget(max_tokens=1000)
        call_count = 0

        while True:
            try:
                budget.consume(tokens=200, cost_usd=0.001)
                call_count += 1
            except BudgetExceeded:
                break

        assert call_count == 5  # 5 * 200 = 1000, which is not > 1000, so 6th call fails
        # Actually: 5 calls use 1000 tokens, which is not > 1000. The 6th would be 1200 > 1000.
        # But the loop checks AFTER incrementing, so it stops on the 6th attempt.
        # Let me recalculate: after 5 calls, tokens_used = 1000. 6th call: 1200 > 1000 → raises.
        # So call_count = 5 when we break.

    def test_cost_budget_stops_loop(self):
        """A run that burns cost in a loop is stopped by the cost budget."""
        budget = RunBudget(max_cost_usd=0.10)
        call_count = 0

        while True:
            try:
                budget.consume(tokens=10, cost_usd=0.03)
                call_count += 1
            except BudgetExceeded:
                break

        # After 3 calls: cost = 0.09 (not > 0.10). 4th call: 0.12 > 0.10 → raises.
        assert call_count == 3


# ---------------------------------------------------------------------------
# 2. Failure Case 2: Duplicate emails sent (idempotency prevents this)
# ---------------------------------------------------------------------------


class TestIdempotencyPreventsDuplicates:
    @pytest.mark.asyncio
    async def test_retry_does_not_send_duplicate_email(self):
        """A tool that times out, then succeeds on retry, should send ONE email.

        Course Case 2: 'Duplicate emails sent — retry policy had no idempotency.'
        Fix: IdempotentToolExecutor wraps the tool call."""
        store = InMemoryStateStore()
        executor = IdempotentToolExecutor(store=store)

        send_count = 0

        async def flaky_send_email(to, subject, body):
            nonlocal send_count
            send_count += 1
            if send_count < 2:
                raise TimeoutError("network timeout on first attempt")
            return {"message_id": "msg_001", "status": "sent"}

        # Wrap the flaky tool in retry_with_backoff.
        # This function accepts the same args as flaky_send_email and
        # internally retries on TimeoutError.
        async def retried_send_email(to, subject, body):
            return await retry_with_backoff(
                flaky_send_email, to, subject, body,
                max_attempts=3, base_delay=0.01,
            )

        args = {"to": "r@acme.com", "subject": "Hi", "body": "Hello"}

        # First call — retry handles the timeout, then idempotency caches the result
        result = await executor.call(
            tool=retried_send_email,
            workspace_id="ws_1", run_id="run_1", step_id=1,
            tool_name="send_email",
            args=args,
        )
        assert result["status"] == "sent"
        # The tool was called twice (first timed out, second succeeded)
        assert send_count == 2

        # If we retry the SAME call (same idempotency key), it returns cached
        # result WITHOUT re-executing — no duplicate email.
        send_count_before = send_count
        result2 = await executor.call(
            tool=retried_send_email,
            workspace_id="ws_1", run_id="run_1", step_id=1,
            tool_name="send_email",
            args=args,
        )
        assert send_count == send_count_before  # no additional execution
        assert result2["message_id"] == "msg_001"  # same result

    @pytest.mark.asyncio
    async def test_two_parallel_calls_one_executes(self):
        """Two workers calling the same tool at the same time — only one executes.

        Course Case 6: 'Race condition on shared state.'"""
        store = InMemoryStateStore()
        executor = IdempotentToolExecutor(store=store)

        call_count = 0

        async def send_email(to, subject, body):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)  # simulate slow provider
            return {"message_id": "msg_001", "status": "sent"}

        args = {"to": "r@acme.com", "subject": "Hi", "body": "Hello"}

        # Two workers call simultaneously
        try:
            results = await asyncio.gather(
                executor.call(tool=send_email, workspace_id="ws_1",
                             run_id="run_1", step_id=1, tool_name="send_email",
                             args=args),
                executor.call(tool=send_email, workspace_id="ws_1",
                             run_id="run_1", step_id=1, tool_name="send_email",
                             args=args),
                return_exceptions=True,
            )
        except Exception:
            pass

        # One should succeed, the other should get InProgressError or cached result
        # Either way, the tool was called at most once
        assert call_count <= 1


# ---------------------------------------------------------------------------
# 3. Failure Case 5: Provider outage mid-run (retry handles this)
# ---------------------------------------------------------------------------


class TestProviderOutage:
    @pytest.mark.asyncio
    async def test_503_then_200_retries_and_succeeds(self):
        """Provider returns 503 for 2 calls, then 200. Retry should succeed.

        Course Case 5: 'Provider outage mid-run — 30 runs fail in 5 min.'
        Fix: retry_with_backoff catches TransientError (5xx wrapper)."""
        call_count = 0

        async def flaky_provider():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise TransientError("503 service unavailable")
            return {"status": "ok"}

        result = await retry_with_backoff(
            flaky_provider, max_attempts=4, base_delay=0.01
        )
        assert result["status"] == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_429_with_retry_after_honored(self):
        """Provider returns 429 with Retry-After: 0.01. Retry honors it."""
        call_count = 0

        async def rate_limited():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RateLimitError("429 too many requests", retry_after=0.01)
            return "ok"

        result = await retry_with_backoff(rate_limited, max_attempts=3, base_delay=0.001)
        assert result == "ok"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_all_attempts_fail_raises_retry_exhausted(self):
        """Provider is down for all 4 attempts → RetryExhausted."""

        async def always_down():
            raise TransientError("503")

        with pytest.raises(RetryExhausted) as exc_info:
            await retry_with_backoff(always_down, max_attempts=4, base_delay=0.01)
        assert exc_info.value.attempts == 4


# ---------------------------------------------------------------------------
# 4. Failure Case 7: Tool argument attack (allowlist prevents this)
# ---------------------------------------------------------------------------


class TestToolArgAllowlist:
    @pytest.mark.asyncio
    async def test_crm_status_allowlist_rejects_admin(self):
        """The MockCRMProvider rejects status='admin' (not in allowlist).

        Course Case 7: 'Agent calls update_lead_status(status=admin).'
        Fix: ALLOWED_STATUSES in MockCRMProvider."""
        from opsmesh.adapters.crm.mock import MockCRMProvider, ALLOWED_STATUSES, ToolError
        from opsmesh.adapters.crm.base import CRMLead

        crm = MockCRMProvider()
        lead = CRMLead(
            lead_id="L_001", workspace_id="ws_1",
            company="Acme", contact_email="r@acme.com",
        )
        crm.add_lead(lead)

        with pytest.raises(ToolError) as exc_info:
            await crm.update_lead_status("ws_1", "L_001", "admin")
        assert exc_info.value.code == "invalid_status"

    def test_allowlist_contains_exactly_documented_statuses(self):
        from opsmesh.adapters.crm.mock import ALLOWED_STATUSES
        assert ALLOWED_STATUSES == {
            "new", "contacted", "qualified", "rejected",
            "disqualified", "needs_review",
        }


# ---------------------------------------------------------------------------
# 5. Budget exhaustion is visible (not silent)
# ---------------------------------------------------------------------------


class TestBudgetVisibility:
    def test_budget_exceeded_carries_used_and_limits(self):
        """BudgetExceeded includes the used and limits dicts for debugging."""
        budget = RunBudget(max_tokens=100, max_cost_usd=0.10)
        budget.consume(tokens=50, cost_usd=0.05)

        with pytest.raises(BudgetExceeded) as exc_info:
            budget.consume(tokens=60, cost_usd=0.0)

        # The exception carries full context
        assert exc_info.value.used["tokens"] == 110
        assert exc_info.value.limits["max_tokens"] == 100
        assert "tokens" in exc_info.value.reason

    def test_budget_to_dict_for_dashboard(self):
        """to_dict() provides all fields the dashboard needs."""
        budget = RunBudget()
        budget.consume(tokens=500, cost_usd=0.01)
        budget.tool_call()

        data = budget.to_dict()
        assert data["tokens_used"] == 500
        assert data["cost_usd"] == 0.01
        assert data["tool_calls"] == 1
        assert "remaining_pct" in data
        assert "max_tokens" in data
        assert "max_cost_usd" in data


# ---------------------------------------------------------------------------
# 6. Combined: retry + idempotency (the key reliability pattern)
# ---------------------------------------------------------------------------


class TestRetryPlusIdempotency:
    @pytest.mark.asyncio
    async def test_crash_during_send_then_retry_no_duplicate(self):
        """Simulate: worker crashes mid-send. On restart, the idempotency
        record is PENDING. The retry picks up and completes without
        sending a duplicate.

        This is the MOST IMPORTANT reliability test in the system:
          - Crash recovery + idempotency + no duplicate side effects.
        """
        store = InMemoryStateStore()

        # Pre-insert a PENDING idempotency record (simulating a crash mid-send)
        args = {"to": "r@acme.com", "subject": "Hi", "body": "Hello"}
        key = make_idempotency_key("ws_1", "run_1", 1, "send_email", args)
        await store.insert_idempotency(IdempotencyRecord(
            key=key, workspace_id="ws_1", run_id="run_1",
            step_id=1, tool_name="send_email",
            status=IdempotencyStatus.PENDING,
        ))

        executor = IdempotentToolExecutor(store=store)
        send_count = 0

        async def send_email(to, subject, body):
            nonlocal send_count
            send_count += 1
            return {"message_id": "msg_001", "status": "sent"}

        # The executor sees PENDING → raises InProgressError
        with pytest.raises(InProgressError):
            await executor.call(
                tool=send_email,
                workspace_id="ws_1", run_id="run_1", step_id=1,
                tool_name="send_email", args=args,
            )

        # The tool was NOT called (PENDING means another worker is on it)
        assert send_count == 0

        # Now simulate the original worker completing: update to DONE
        await store.update_idempotency(
            key, IdempotencyStatus.DONE,
            result={"message_id": "msg_001", "status": "sent"},
        )

        # The retrying worker calls again — now it gets the cached result
        result = await executor.call(
            tool=send_email,
            workspace_id="ws_1", run_id="run_1", step_id=1,
            tool_name="send_email", args=args,
        )

        # The tool was STILL NOT called (cache hit)
        assert send_count == 0
        # The result is the cached one
        assert result["message_id"] == "msg_001"

    @pytest.mark.asyncio
    async def test_failed_then_retry_succeeds(self):
        """A tool fails, then on retry it succeeds. The idempotency record
        goes FAILED → PENDING → DONE."""
        store = InMemoryStateStore()
        executor = IdempotentToolExecutor(store=store)

        call_count = 0

        async def flaky_tool(value):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first attempt fails")
            return {"value": value, "attempt": call_count}

        args = {"value": 42}

        # First call — fails
        with pytest.raises(RuntimeError):
            await executor.call(
                tool=flaky_tool,
                workspace_id="ws_1", run_id="run_1", step_id=1,
                tool_name="flaky", args=args,
            )
        assert call_count == 1

        # Retry — the FAILED record is updated to PENDING, tool re-executes
        result = await executor.call(
            tool=flaky_tool,
            workspace_id="ws_1", run_id="run_1", step_id=1,
            tool_name="flaky", args=args,
        )
        assert call_count == 2
        assert result["value"] == 42

        # Third call — DONE record, returns cached result
        result3 = await executor.call(
            tool=flaky_tool,
            workspace_id="ws_1", run_id="run_1", step_id=1,
            tool_name="flaky", args=args,
        )
        assert call_count == 2  # not re-executed
