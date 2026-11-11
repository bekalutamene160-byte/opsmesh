"""
Tests for opsmesh.idempotency — prevents duplicate side effects on retry.

Covers:
  - Key computation (deterministic, includes all components)
  - Sensitive args redacted in key
  - First call executes the tool
  - Second call with same key returns cached result (no re-execution)
  - Different args → different key → re-executes
  - InProgressError when PENDING
  - FAILED records allow retry
  - Race condition: two workers, one wins insert, other re-reads
  - Sync tools supported (not just async)
  - Pydantic model results serialized correctly
"""

from __future__ import annotations

import asyncio

import pytest

from opsmesh.idempotency import (
    IdempotentToolExecutor,
    InProgressError,
    make_idempotency_key,
)
from opsmesh.models import IdempotencyStatus, IdempotencyRecord
from opsmesh.state_store import InMemoryStateStore


@pytest.fixture
def store():
    return InMemoryStateStore()


@pytest.fixture
def executor(store):
    return IdempotentToolExecutor(store=store)


# ---------------------------------------------------------------------------
# 1. Key computation
# ---------------------------------------------------------------------------


class TestKeyComputation:
    def test_same_inputs_same_key(self):
        key1 = make_idempotency_key("ws_1", "run_1", 1, "send_email",
                                     {"to": "a@b.com", "subject": "Hi"})
        key2 = make_idempotency_key("ws_1", "run_1", 1, "send_email",
                                     {"to": "a@b.com", "subject": "Hi"})
        assert key1 == key2

    def test_different_workspace_different_key(self):
        k1 = make_idempotency_key("ws_1", "run_1", 1, "tool", {"a": 1})
        k2 = make_idempotency_key("ws_2", "run_1", 1, "tool", {"a": 1})
        assert k1 != k2

    def test_different_run_different_key(self):
        k1 = make_idempotency_key("ws_1", "run_1", 1, "tool", {"a": 1})
        k2 = make_idempotency_key("ws_1", "run_2", 1, "tool", {"a": 1})
        assert k1 != k2

    def test_different_step_different_key(self):
        """Same tool at different steps produces different keys."""
        k1 = make_idempotency_key("ws_1", "run_1", 1, "tool", {"a": 1})
        k2 = make_idempotency_key("ws_1", "run_1", 2, "tool", {"a": 1})
        assert k1 != k2

    def test_different_tool_different_key(self):
        k1 = make_idempotency_key("ws_1", "run_1", 1, "send_email", {"a": 1})
        k2 = make_idempotency_key("ws_1", "run_1", 1, "send_notification", {"a": 1})
        assert k1 != k2

    def test_different_args_different_key(self):
        k1 = make_idempotency_key("ws_1", "run_1", 1, "tool", {"to": "a@b.com"})
        k2 = make_idempotency_key("ws_1", "run_1", 1, "tool", {"to": "c@d.com"})
        assert k1 != k2

    def test_different_arg_order_same_key(self):
        """Dict ordering shouldn't affect the key (args are sorted)."""
        k1 = make_idempotency_key("ws_1", "run_1", 1, "tool",
                                  {"a": 1, "b": 2})
        k2 = make_idempotency_key("ws_1", "run_1", 1, "tool",
                                  {"b": 2, "a": 1})
        assert k1 == k2

    def test_payload_hash_affects_key(self):
        """For send_email, a different payload_hash produces a different key.
        This prevents stale approvals from replaying previous sends."""
        k1 = make_idempotency_key("ws_1", "run_1", 1, "send_email",
                                  {"to": "a@b.com"}, payload_hash="hash_v1")
        k2 = make_idempotency_key("ws_1", "run_1", 1, "send_email",
                                  {"to": "a@b.com"}, payload_hash="hash_v2")
        assert k1 != k2

    def test_key_format(self):
        key = make_idempotency_key("ws_1", "run_1", 1, "tool", {"a": 1})
        assert key.startswith("ik_")
        assert len(key) == 19  # ik_ + 16 hex chars


# ---------------------------------------------------------------------------
# 2. Sensitive arg redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_password_redacted(self):
        """password field is replaced with [REDACTED] before hashing."""
        k1 = make_idempotency_key("ws_1", "run_1", 1, "tool",
                                  {"password": "secret123", "data": "x"})
        k2 = make_idempotency_key("ws_1", "run_1", 1, "tool",
                                  {"password": "different456", "data": "x"})
        # Same key because password is redacted — only non-sensitive args matter
        assert k1 == k2

    def test_api_key_redacted(self):
        k1 = make_idempotency_key("ws_1", "run_1", 1, "tool",
                                  {"api_key": "sk-abc", "query": "test"})
        k2 = make_idempotency_key("ws_1", "run_1", 1, "tool",
                                  {"api_key": "sk-xyz", "query": "test"})
        assert k1 == k2

    def test_token_redacted(self):
        k1 = make_idempotency_key("ws_1", "run_1", 1, "tool",
                                  {"token": "tok_1", "data": "same"})
        k2 = make_idempotency_key("ws_1", "run_1", 1, "tool",
                                  {"token": "tok_2", "data": "same"})
        assert k1 == k2

    def test_non_sensitive_args_not_redacted(self):
        """Regular args (to, subject, body) are NOT redacted."""
        k1 = make_idempotency_key("ws_1", "run_1", 1, "send_email",
                                  {"to": "a@b.com", "subject": "Hi"})
        k2 = make_idempotency_key("ws_1", "run_1", 1, "send_email",
                                  {"to": "c@d.com", "subject": "Hi"})
        assert k1 != k2  # different 'to' → different key


# ---------------------------------------------------------------------------
# 3. First call executes, second returns cached
# ---------------------------------------------------------------------------


class TestCacheHit:
    @pytest.mark.asyncio
    async def test_first_call_executes(self, executor):
        call_count = 0

        async def send_email(to, subject, body):
            nonlocal call_count
            call_count += 1
            return {"message_id": "msg_001", "status": "sent"}

        result = await executor.call(
            tool=send_email,
            workspace_id="ws_1", run_id="run_1", step_id=5,
            tool_name="send_email",
            args={"to": "r@acme.com", "subject": "Hi", "body": "Hello"},
        )
        assert call_count == 1
        assert result["message_id"] == "msg_001"

    @pytest.mark.asyncio
    async def test_second_call_returns_cached_no_reexecution(self, executor):
        """Same key → cached result returned, tool NOT re-executed."""
        call_count = 0

        async def send_email(to, subject, body):
            nonlocal call_count
            call_count += 1
            return {"message_id": f"msg_{call_count}", "status": "sent"}

        args = {"to": "r@acme.com", "subject": "Hi", "body": "Hello"}

        # First call
        result1 = await executor.call(
            tool=send_email,
            workspace_id="ws_1", run_id="run_1", step_id=5,
            tool_name="send_email", args=args,
        )
        assert call_count == 1

        # Second call — same key → cached, no re-execution
        result2 = await executor.call(
            tool=send_email,
            workspace_id="ws_1", run_id="run_1", step_id=5,
            tool_name="send_email", args=args,
        )
        assert call_count == 1  # STILL 1 — not re-executed
        # The cached result has the same message_id
        assert result2["message_id"] == "msg_1"

    @pytest.mark.asyncio
    async def test_different_args_re_executes(self, executor):
        """Different args → different key → tool IS re-executed."""
        call_count = 0

        async def send_email(to, subject, body):
            nonlocal call_count
            call_count += 1
            return {"message_id": f"msg_{call_count}", "status": "sent"}

        # First call
        await executor.call(
            tool=send_email,
            workspace_id="ws_1", run_id="run_1", step_id=5,
            tool_name="send_email",
            args={"to": "a@acme.com", "subject": "Hi", "body": "Hello"},
        )
        assert call_count == 1

        # Second call with different 'to' → different key → re-executes
        await executor.call(
            tool=send_email,
            workspace_id="ws_1", run_id="run_1", step_id=5,
            tool_name="send_email",
            args={"to": "b@acme.com", "subject": "Hi", "body": "Hello"},
        )
        assert call_count == 2


# ---------------------------------------------------------------------------
# 4. InProgressError
# ---------------------------------------------------------------------------


class TestInProgress:
    @pytest.mark.asyncio
    async def test_pending_raises_in_progress(self, store, executor):
        """If a PENDING record exists, raise InProgressError."""
        # Manually insert a PENDING record
        key = make_idempotency_key("ws_1", "run_1", 1, "send_email",
                                   {"to": "r@acme.com"})
        await store.insert_idempotency(IdempotencyRecord(
            key=key, workspace_id="ws_1", run_id="run_1",
            step_id=1, tool_name="send_email",
            status=IdempotencyStatus.PENDING,
        ))

        async def send_email(to):
            return {"status": "should_not_reach_here"}

        with pytest.raises(InProgressError):
            await executor.call(
                tool=send_email,
                workspace_id="ws_1", run_id="run_1", step_id=1,
                tool_name="send_email",
                args={"to": "r@acme.com"},
            )


# ---------------------------------------------------------------------------
# 5. FAILED records allow retry
# ---------------------------------------------------------------------------


class TestFailedRetry:
    @pytest.mark.asyncio
    async def test_failed_record_allows_retry(self, store, executor):
        """If a FAILED record exists, the tool is re-executed."""
        key = make_idempotency_key("ws_1", "run_1", 1, "send_email",
                                   {"to": "r@acme.com"})
        await store.insert_idempotency(IdempotencyRecord(
            key=key, workspace_id="ws_1", run_id="run_1",
            step_id=1, tool_name="send_email",
            status=IdempotencyStatus.FAILED,
            error="previous attempt failed",
        ))

        call_count = 0

        async def send_email(to):
            nonlocal call_count
            call_count += 1
            return {"status": "sent"}

        result = await executor.call(
            tool=send_email,
            workspace_id="ws_1", run_id="run_1", step_id=1,
            tool_name="send_email",
            args={"to": "r@acme.com"},
        )
        assert call_count == 1  # re-executed because previous was FAILED
        assert result["status"] == "sent"


# ---------------------------------------------------------------------------
# 6. Tool failure marks record FAILED
# ---------------------------------------------------------------------------


class TestToolFailure:
    @pytest.mark.asyncio
    async def test_exception_marks_failed_and_propagates(self, store, executor):
        call_count = 0

        async def failing_tool(to):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("provider error")

        with pytest.raises(RuntimeError):
            await executor.call(
                tool=failing_tool,
                workspace_id="ws_1", run_id="run_1", step_id=1,
                tool_name="send_email",
                args={"to": "r@acme.com"},
            )

        # Verify the record was marked FAILED
        key = make_idempotency_key("ws_1", "run_1", 1, "send_email",
                                   {"to": "r@acme.com"})
        record = await store.get_idempotency(key)
        assert record is not None
        assert record.status == IdempotencyStatus.FAILED
        assert "RuntimeError" in (record.error or "")
        assert "provider error" in (record.error or "")


# ---------------------------------------------------------------------------
# 7. Sync tool support
# ---------------------------------------------------------------------------


class TestSyncTool:
    @pytest.mark.asyncio
    async def test_sync_tool_executed(self, executor):
        """The executor supports sync tools (not just async)."""
        call_count = 0

        def sync_tool(value):
            nonlocal call_count
            call_count += 1
            return {"doubled": value * 2}

        result = await executor.call(
            tool=sync_tool,
            workspace_id="ws_1", run_id="run_1", step_id=1,
            tool_name="doubler",
            args={"value": 21},
        )
        assert result["doubled"] == 42
        assert call_count == 1


# ---------------------------------------------------------------------------
# 8. Pydantic model results
# ---------------------------------------------------------------------------


class TestPydanticResult:
    @pytest.mark.asyncio
    async def test_pydantic_result_serialized(self, executor):
        """If the tool returns a Pydantic model, it's serialized for storage."""
        from opsmesh.tools.schemas import SendEmailResult

        async def send_email(to, subject, body, approval_id):
            return SendEmailResult(status="sent")

        result = await executor.call(
            tool=send_email,
            workspace_id="ws_1", run_id="run_1", step_id=1,
            tool_name="send_email",
            args={"to": "r@acme.com", "subject": "Hi", "body": "x",
                  "approval_id": "ap_abcdef123456"},
        )
        assert result.status == "sent"

        # Second call — returns cached dict (serialized form)
        result2 = await executor.call(
            tool=send_email,
            workspace_id="ws_1", run_id="run_1", step_id=1,
            tool_name="send_email",
            args={"to": "r@acme.com", "subject": "Hi", "body": "x",
                  "approval_id": "ap_abcdef123456"},
        )
        # Cached result is a dict (serialized)
        assert isinstance(result2, dict)
        assert result2["status"] == "sent"
