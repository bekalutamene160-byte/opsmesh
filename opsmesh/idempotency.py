"""
OPSMESH IdempotentToolExecutor — prevents duplicate side effects on retry.

Architecture (course Ch 9 + Correction #8 in OPSMESH_Corrections):
  Retries are necessary for reliability. But retries + side effects = duplicate
  emails, double-billed charges, corrupted CRM records. Idempotency makes side
  effects safe to retry.

THE PATTERN:
  1. Compute an idempotency key from (workspace_id, run_id, step_id,
     tool_name, args, payload_hash).
  2. Look up the key in the idempotency table.
     - If DONE: return the cached result. DO NOT re-execute.
     - If PENDING: another worker is executing. Raise InProgressError.
     - If FAILED: allow retry (clear and re-execute).
  3. Insert a PENDING row. Atomic: ON CONFLICT DO NOTHING.
     - If insert returns False: another worker won. Re-read.
  4. Execute the tool.
  5. Update the row to DONE (with result) or FAILED (with error).

CRITICAL (Correction #8): Insert the PENDING row BEFORE calling the tool.
  If you insert AFTER, a crash between the call and the insert leaves you
  with no record — did the email send or not? Unknown. With the PENDING
  row inserted first, a crash leaves a PENDING row that the reconciler
  can investigate.

KEY COMPOSITION — why each component is included:
  workspace_id  : different workspaces can do same action independently
  run_id        : different runs can do same action
  step_id       : same tool at different points in workflow produces
                  different keys (avoids overwriting first draft with second)
  tool_name     : different tools have different effects even with same args
  canonical args: same logical call → same key (sorted, no dict ordering issues)
  payload_hash  : for send_email, a re-approved draft must produce different key
                  (so a stale approval cannot replay a previous send)

WHAT REDACT MEANS:
  Sensitive fields (password, secret, token, api_key) in args are replaced
  with "[REDACTED]" before hashing. This prevents secrets from leaking into
  the idempotency key (which is stored in the DB).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Callable, Optional

from opsmesh.models import IdempotencyRecord, IdempotencyStatus
from opsmesh.state_store import StateStore


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IdempotencyError(Exception):
    """Base class for idempotency errors."""


class InProgressError(IdempotencyError):
    """Another worker is executing this tool call. Caller should wait or
    return "in-progress" to the API caller."""


# ---------------------------------------------------------------------------
# Key computation
# ---------------------------------------------------------------------------


# Substrings that indicate a sensitive field name (case-insensitive).
# Values of these fields are redacted to "[REDACTED]" before hashing.
_SENSITIVE_SUBSTRINGS = ("password", "secret", "token", "api_key", "apikey")


def _redact_and_sort_args(args: dict) -> dict:
    """Sort args alphabetically and redact sensitive field values.

    Redaction prevents secrets (API keys, passwords) from leaking into the
    idempotency key, which is stored in the DB.

    Sorting ensures the same dict produces the same key regardless of
    insertion order (Python 3.7+ preserves insertion order, but different
    callers may construct dicts in different orders).
    """
    redacted = {}
    for key in sorted(args.keys()):
        value = args[key]
        if any(s in key.lower() for s in _SENSITIVE_SUBSTRINGS):
            redacted[key] = "[REDACTED]"
        else:
            redacted[key] = value
    return redacted


def make_idempotency_key(
    workspace_id: str,
    run_id: str,
    step_id: int,
    tool_name: str,
    args: dict,
    payload_hash: Optional[str] = None,
) -> str:
    """
    Compute a deterministic idempotency key.

    Same inputs → same key. Different inputs → different key.
    The key is "ik_" + first 16 hex chars of SHA256.

    Args:
        workspace_id: the workspace owning this run.
        run_id: the run this tool call belongs to.
        step_id: the step number within the run (for multi-call steps).
        tool_name: the tool being called (e.g., "send_email").
        args: the tool's arguments (will be sorted + redacted).
        payload_hash: for send_email, the hash of the approved payload.
            None for non-approval tools. Including this ensures a
            re-approved draft produces a different key (so a stale
            approval can't replay a previous send).

    Returns:
        A 19-char string like "ik_abcdef0123456789".
    """
    canonical = json.dumps(
        {
            "workspace_id": workspace_id,
            "run_id": run_id,
            "step_id": step_id,
            "tool": tool_name,
            "args": _redact_and_sort_args(args),
            "payload_hash": payload_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "ik_" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# IdempotentToolExecutor
# ---------------------------------------------------------------------------


class IdempotentToolExecutor:
    """
    Wraps a tool call with idempotency protection.

    Usage:
        executor = IdempotentToolExecutor(store=state_store)
        result = await executor.call(
            tool=send_email_func,
            workspace_id="ws_1",
            run_id="run_1",
            step_id=5,
            tool_name="send_email",
            args={"to": "r@acme.com", "subject": "Hi", "body": "Hello"},
        )

    What happens:
        1. Compute the idempotency key.
        2. Check if a DONE record exists → return cached result (no re-execution).
        3. Check if a PENDING record exists → raise InProgressError.
        4. Insert a PENDING record (atomic; on race, re-read).
        5. Call the tool.
        6. Update the record to DONE (with result) or FAILED (with error).
    """

    def __init__(self, store: StateStore):
        self.store = store

    async def call(
        self,
        tool: Callable,
        workspace_id: str,
        run_id: str,
        step_id: int,
        tool_name: str,
        args: dict,
        payload_hash: Optional[str] = None,
    ) -> Any:
        """
        Execute a tool call with idempotency protection.

        Returns:
            The tool's result on first call, or the cached result on
            subsequent calls with the same key.

        Raises:
            InProgressError: if another worker is executing the same call.
            Any exception from the tool: propagated after the record is
            marked FAILED.
        """
        key = make_idempotency_key(
            workspace_id, run_id, step_id, tool_name, args, payload_hash
        )

        # 1. Look up existing record
        existing = await self.store.get_idempotency(key)
        if existing is not None:
            if existing.status == IdempotencyStatus.DONE:
                # Cache hit — return the stored result. DO NOT re-execute.
                return existing.result
            if existing.status == IdempotencyStatus.PENDING:
                # Another worker is executing this call right now.
                raise InProgressError(
                    f"Tool call {key} ({tool_name}) is already in progress "
                    f"by another worker"
                )
            # If FAILED, update to PENDING and re-execute
            await self.store.update_idempotency(key, IdempotencyStatus.PENDING)
        else:
            # 2. No existing record — insert a PENDING record (atomic)
            record = IdempotencyRecord(
                key=key,
                workspace_id=workspace_id,
                run_id=run_id,
                step_id=step_id,
                tool_name=tool_name,
                status=IdempotencyStatus.PENDING,
            )
            inserted = await self.store.insert_idempotency(record)
            if not inserted:
                # Race condition: another worker inserted between our get and
                # our insert. Re-read to see their result.
                existing = await self.store.get_idempotency(key)
                if existing is not None:
                    if existing.status == IdempotencyStatus.DONE:
                        return existing.result
                    if existing.status == IdempotencyStatus.PENDING:
                        raise InProgressError(
                            f"Tool call {key} ({tool_name}) is in progress "
                            f"after race condition"
                        )
                # If still not found (shouldn't happen), re-execute
                return await self.call(
                    tool, workspace_id, run_id, step_id, tool_name, args, payload_hash
                )

        # 3. Execute the tool
        try:
            # Support both async and sync tools
            if asyncio.iscoroutinefunction(tool):
                result = await tool(**args)
            else:
                result = tool(**args)

            # Serialize the result for storage
            stored_result = self._serialize_result(result)
            await self.store.update_idempotency(
                key, IdempotencyStatus.DONE, result=stored_result
            )
            return result

        except Exception as e:
            # Mark the record as FAILED with a sanitized error message
            safe_error = f"{type(e).__name__}: {str(e)[:200]}"
            await self.store.update_idempotency(
                key, IdempotencyStatus.FAILED, error=safe_error
            )
            raise

    def _serialize_result(self, result: Any) -> dict:
        """Serialize a tool result for storage in the idempotency table.

        Handles Pydantic models, dicts, and plain values.
        """
        if hasattr(result, "model_dump"):
            # Pydantic v2 model
            return result.model_dump(mode="json")
        if isinstance(result, dict):
            return result
        if isinstance(result, (str, int, float, bool)):
            return {"value": result}
        # Fallback: stringify
        return {"value": str(result)}


__all__ = [
    "IdempotencyError",
    "InProgressError",
    "make_idempotency_key",
    "IdempotentToolExecutor",
]
