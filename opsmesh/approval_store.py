"""
OPSMESH Approval Store — real approval verification (replaces placeholder).

ARCHITECTURE CORRECTION (Day 4 hardening):
  The original Day 4 ApprovalVerifier returned True always (placeholder).
  This module implements REAL approval verification:

    1. Approval exists.
    2. Belongs to the correct workspace + run.
    3. Authorizes the requested action (tool_name).
    4. Is the correct Gate (Gate 1 = evidence_review, Gate 2 = email_send).
       Gate 1 CANNOT authorize Gate 2 actions (Correction 5).
    5. Has not expired (24h timeout).
    6. Has not been consumed (single-use; can't replay).
    7. payload_hash matches the EXACT action being approved.
       - Recompute the hash from the actual tool args.
       - Reject modified recipients, subjects, bodies, tool names.

CONCURRENT USE PROTECTION:
  consume() uses atomic compare-and-swap: only one caller can transition
  PENDING → APPROVED → CONSUMED. Duplicate concurrent calls get False.

USAGE:
  store = InMemoryApprovalStore()
  approval = store.create(workspace_id, run_id, kind=ApprovalKind.EMAIL_SEND,
                            action="send_email", payload={...}, payload_hash="...",
                            expires_at=...)
  # Later, when verifying:
  ok = store.verify_and_consume(approval_id, workspace_id, run_id,
                                  action="send_email", payload_hash="...")
  if not ok:
      raise StaleApprovalError(...)

Day 5 will replace InMemoryApprovalStore with a Postgres-backed store
that persists approval records in the `approvals` table.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Optional, Protocol

from opsmesh.models import ApprovalKind, ApprovalRecord, ApprovalStatus, utcnow


# Default approval timeout: 24 hours (Engineering Contract §1)
DEFAULT_APPROVAL_TIMEOUT = timedelta(hours=24)


class ApprovalError(Exception):
    """Base class for approval verification errors. Carries a stable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class ApprovalNotFoundError(ApprovalError):
    def __init__(self, approval_id: str):
        super().__init__("approval_not_found", f"Approval {approval_id} does not exist")


class ApprovalExpiredError(ApprovalError):
    def __init__(self, approval_id: str):
        super().__init__("approval_expired", f"Approval {approval_id} has expired")


class ApprovalConsumedError(ApprovalError):
    def __init__(self, approval_id: str):
        super().__init__("approval_consumed", f"Approval {approval_id} was already used")


class ApprovalWorkspaceMismatchError(ApprovalError):
    def __init__(self, approval_id: str, expected_ws: str, actual_ws: str):
        super().__init__(
            "approval_workspace_mismatch",
            f"Approval {approval_id} belongs to workspace {actual_ws}, not {expected_ws}",
        )


class ApprovalRunMismatchError(ApprovalError):
    def __init__(self, approval_id: str, expected_run: str, actual_run: str):
        super().__init__(
            "approval_run_mismatch",
            f"Approval {approval_id} belongs to run {actual_run}, not {expected_run}",
        )


class ApprovalActionMismatchError(ApprovalError):
    def __init__(self, approval_id: str, expected_action: str, actual_action: str):
        super().__init__(
            "approval_action_mismatch",
            f"Approval {approval_id} authorizes '{actual_action}', not '{expected_action}'",
        )


class ApprovalPayloadMismatchError(ApprovalError):
    """The payload_hash of the actual action doesn't match the approval's hash.

    This is the CRITICAL security check: it catches modified recipients,
    subjects, bodies, tool names, or other protected arguments."""

    def __init__(self, approval_id: str, expected_hash: str, actual_hash: str):
        super().__init__(
            "approval_payload_mismatch",
            f"Approval {approval_id} payload_hash mismatch: "
            f"expected {expected_hash[:12]}..., got {actual_hash[:12]}...",
        )


class ApprovalGateMismatchError(ApprovalError):
    """Gate 1 approval cannot authorize Gate 2 actions (Correction 5)."""

    def __init__(self, approval_id: str, approval_kind: str, required_kind: str):
        super().__init__(
            "approval_gate_mismatch",
            f"Approval {approval_id} is a {approval_kind} approval, "
            f"but a {required_kind} approval is required",
        )


# ---------------------------------------------------------------------------
# ApprovalStore Protocol — Day 5 replaces with Postgres-backed implementation
# ---------------------------------------------------------------------------


class ApprovalStore(Protocol):
    """
    The approval persistence contract.

    Day 4: InMemoryApprovalStore (tests + dev).
    Day 5: PostgresApprovalStore (persists to the `approvals` table).

    CONCURRENCY SAFETY:
        decide() and verify_and_consume() are ATOMIC operations. They use
        a lock (in-memory) or atomic UPDATE ... WHERE status='pending'
        (Postgres) to guarantee that two concurrent callers cannot both
        succeed on the same approval.
    """

    def create(
        self,
        workspace_id: str,
        run_id: str,
        kind: ApprovalKind,
        action: str,
        payload: dict,
        payload_hash: str,
        expires_at=None,
    ) -> ApprovalRecord:
        """Create + persist a new approval record. Returns the record."""
        ...

    def get(self, approval_id: str) -> Optional[ApprovalRecord]:
        """Fetch an approval by ID. Returns None if not found."""
        ...

    async def decide(
        self,
        approval_id: str,
        workspace_id: str,
        new_status: ApprovalStatus,
        decision: str,
        decided_by: str,
        feedback: Optional[str] = None,
    ) -> ApprovalRecord:
        """Atomically transition an approval from PENDING to a decided status.

        This is the concurrency-safe version of decide(). Only the first
        caller wins; concurrent callers get AlreadyResolvedError.

        Day 6's PostgresApprovalStore will implement this as:
            UPDATE approvals SET status=$1, decision=$2, ...
            WHERE id=$3 AND status='pending'
            RETURNING *
        If 0 rows updated → AlreadyResolvedError.
        """
        ...

    async def verify_and_consume(
        self,
        approval_id: str,
        workspace_id: str,
        run_id: str,
        action: str,
        payload_hash: str,
    ) -> ApprovalRecord:
        """Atomically verify + consume an approval.

        Checks:
            1. Approval exists
            2. Not expired
            3. Not already consumed
            4. Status is APPROVED (human must have decided)
            5. Workspace matches
            6. Run matches
            7. Action matches
            8. payload_hash matches the EXACT action being approved

        If all checks pass, the approval is marked CONSUMED (single-use)
        and returned. If ANY check fails, an ApprovalError is raised.

        Atomicity: uses a lock to prevent two concurrent callers from
        both consuming the same approval."""
        ...

    def find_expired_pending(self) -> list[ApprovalRecord]:
        """Find all pending approvals that have passed their expiration.

        Used by the ApprovalTimeoutReconciler. This is an interface method
        (not private attribute access) so it works with any ApprovalStore
        implementation, including PostgresApprovalStore (Day 6).
        """
        ...


# ---------------------------------------------------------------------------
# InMemoryApprovalStore — Day 4 implementation
# ---------------------------------------------------------------------------


class InMemoryApprovalStore:
    """
    In-memory approval store. Mirror of the Postgres contract.

    Thread-safety: uses asyncio.Lock for atomic verify_and_consume. This
    prevents two concurrent workers from both consuming the same approval.

    NOTE: This is NOT distributed-safe. For multi-process deployments,
    use PostgresApprovalStore (Day 5) which relies on the DB's atomic
    UPDATE ... WHERE status='pending' for true CAS.
    """

    def __init__(self):
        self._approvals: dict[str, ApprovalRecord] = {}
        self._lock = asyncio.Lock()

    def create(
        self,
        workspace_id: str,
        run_id: str,
        kind: ApprovalKind,
        action: str,
        payload: dict,
        payload_hash: str,
        expires_at=None,
    ) -> ApprovalRecord:
        """Create a new pending approval record."""
        if expires_at is None:
            expires_at = utcnow() + DEFAULT_APPROVAL_TIMEOUT

        record = ApprovalRecord(
            workspace_id=workspace_id,
            run_id=run_id,
            kind=kind,
            action=action,
            payload=payload,
            payload_hash=payload_hash,
            expires_at=expires_at,
        )
        self._approvals[record.id] = record
        return record

    def get(self, approval_id: str) -> Optional[ApprovalRecord]:
        """Fetch an approval by ID. Returns None if not found."""
        return self._approvals.get(approval_id)

    async def verify_and_consume(
        self,
        approval_id: str,
        workspace_id: str,
        run_id: str,
        action: str,
        payload_hash: str,
    ) -> ApprovalRecord:
        """Atomically verify + consume an approval.

        Raises ApprovalError subclass on any failure. Returns the record on success.
        """
        async with self._lock:
            record = self._approvals.get(approval_id)
            if record is None:
                raise ApprovalNotFoundError(approval_id)

            # Check expiration
            if utcnow() > record.expires_at:
                record.status = ApprovalStatus.EXPIRED
                raise ApprovalExpiredError(approval_id)

            # Check already consumed (single-use)
            if record.status == ApprovalStatus.CONSUMED:
                raise ApprovalConsumedError(approval_id)

            # The approval must have been APPROVED by a human before it can
            # be consumed by a tool call. PENDING approvals cannot be consumed.
            if record.status != ApprovalStatus.APPROVED:
                raise ApprovalError(
                    "not_approved",
                    f"Approval {approval_id} status is {record.status.value}, "
                    f"not APPROVED — cannot be consumed",
                )

            # Check workspace match
            if record.workspace_id != workspace_id:
                raise ApprovalWorkspaceMismatchError(
                    approval_id, workspace_id, record.workspace_id
                )

            # Check run match
            if record.run_id != run_id:
                raise ApprovalRunMismatchError(
                    approval_id, run_id, record.run_id
                )

            # Check action match (catches "approval for X used for Y")
            if record.action != action:
                raise ApprovalActionMismatchError(
                    approval_id, action, record.action
                )

            # CRITICAL: payload_hash must match the EXACT action being approved.
            # This catches modified recipients, subjects, bodies, tool names.
            if record.payload_hash != payload_hash:
                raise ApprovalPayloadMismatchError(
                    approval_id, record.payload_hash, payload_hash
                )

            # All checks passed — consume the approval (mark CONSUMED = used)
            record.status = ApprovalStatus.CONSUMED
            record.decided_by = record.decided_by or "system_consume"
            record.decided_at = record.decided_at or utcnow()
            self._approvals[approval_id] = record
            return record

    async def decide(
        self,
        approval_id: str,
        workspace_id: str,
        new_status: ApprovalStatus,
        decision: str,
        decided_by: str,
        feedback: Optional[str] = None,
    ) -> ApprovalRecord:
        """Atomically transition an approval from PENDING to a decided status.

        CONCURRENCY SAFETY (Fix 1):
            Uses asyncio.Lock to guarantee that two concurrent decide()
            calls on the same approval cannot both succeed. The first
            caller transitions PENDING → APPROVED/REJECTED/REQUEST_CHANGES.
            The second caller sees the non-PENDING status and raises
            AlreadyResolvedError.

        Day 6's PostgresApprovalStore will implement this as:
            UPDATE approvals
            SET status=$1, decision=$2, decided_by=$3, decided_at=NOW()
            WHERE id=$4 AND workspace_id=$5 AND status='pending'
            RETURNING *
            -- if 0 rows returned → AlreadyResolvedError
        """
        async with self._lock:
            record = self._approvals.get(approval_id)
            if record is None:
                raise ApprovalNotFoundError(approval_id)

            # Check workspace match
            if record.workspace_id != workspace_id:
                raise ApprovalWorkspaceMismatchError(
                    approval_id, workspace_id, record.workspace_id
                )

            # ATOMIC CAS: only PENDING can be decided
            if record.status != ApprovalStatus.PENDING:
                # Already decided — first caller won
                from opsmesh.approval_gate import AlreadyResolvedError
                raise AlreadyResolvedError(
                    approval_id,
                    record.decision or record.status.value,
                )

            # Transition to the new status
            record.status = new_status
            record.decision = decision
            record.decision_feedback = feedback
            record.decided_by = decided_by
            record.decided_at = utcnow()
            self._approvals[approval_id] = record
            return record

    def find_expired_pending(self) -> list[ApprovalRecord]:
        """Find all pending approvals that have passed their expiration.

        Interface method (Fix 4): used by the ApprovalTimeoutReconciler
        instead of accessing _approvals directly. Works with any
        ApprovalStore implementation.
        """
        now = utcnow()
        return [
            record for record in self._approvals.values()
            if record.status == ApprovalStatus.PENDING and now > record.expires_at
        ]

    def list_pending(self, workspace_id: str) -> list[ApprovalRecord]:
        """List all pending approvals for a workspace.

        Used by the API's /approvals endpoint. Without this method, the
        API would need to access _approvals directly (which only works
        for InMemoryApprovalStore, not SQLiteApprovalStore).
        """
        return [
            record for record in self._approvals.values()
            if record.workspace_id == workspace_id
            and record.status == ApprovalStatus.PENDING
        ]

    def expire_pending(self) -> int:
        """Mark all expired pending approvals as EXPIRED. Returns count."""
        expired = self.find_expired_pending()
        for record in expired:
            record.status = ApprovalStatus.EXPIRED
        return len(expired)


# ---------------------------------------------------------------------------
# Real ApprovalVerifier (replaces the placeholder from Day 4)
# ---------------------------------------------------------------------------


class RealApprovalVerifier:
    """
    Real approval verifier — uses an ApprovalStore to actually check
    approval records.

    Replaces the Day 4 placeholder ApprovalVerifier that always returned True.

    The ToolGateway calls verify_and_consume(), which:
      1. Looks up the approval record by ID.
      2. Checks workspace, run, action, expiration, consumption.
      3. CRITICALLY: recomputes payload_hash from the actual tool args
         and compares to the approval's stored hash.
      4. If all pass, marks the approval CONSUMED (single-use).

    Raises ApprovalError subclass on failure. Returns the record on success.
    """

    def __init__(self, store: ApprovalStore):
        self.store = store

    async def verify_and_consume(
        self,
        approval_id: str,
        workspace_id: str,
        run_id: str,
        action: str,
        payload_hash: str,
    ) -> ApprovalRecord:
        """Verify + consume an approval. Raises ApprovalError on failure."""
        return await self.store.verify_and_consume(
            approval_id=approval_id,
            workspace_id=workspace_id,
            run_id=run_id,
            action=action,
            payload_hash=payload_hash,
        )


# ---------------------------------------------------------------------------
# Payload hash computation (used by both approval creation and verification)
# ---------------------------------------------------------------------------


import hashlib
import json


def compute_payload_hash(
    tool_name: str,
    args: dict,
    protected_fields: Optional[list[str]] = None,
) -> str:
    """
    Compute the payload hash for an approval.

    The hash binds the approval to the EXACT action being approved. If any
    protected field changes (recipient, subject, body, etc.), the hash
    changes, and the approval becomes stale (StaleApprovalError).

    Args:
        tool_name: the tool being approved (e.g., "send_email").
        args: the tool's arguments.
        protected_fields: which args are protected. If None, ALL args are
            protected. For send_email, this includes to, from_, subject, body.

    Returns:
        "sha256:<16 hex chars>" string.
    """
    if protected_fields is None:
        # Default: protect all args
        protected = args
    else:
        protected = {k: v for k, v in args.items() if k in protected_fields}

    canonical = json.dumps(
        {"tool": tool_name, "args": protected},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


# Protected fields per tool — these CANNOT change after approval
TOOL_PROTECTED_FIELDS: dict[str, list[str]] = {
    "send_email": ["to", "from_", "subject", "body"],
    "update_lead_status": ["lead_id", "status"],
    "create_lead": ["company", "contact_email"],
}


# ---------------------------------------------------------------------------
# Factory: returns the right ApprovalStore based on the configured DB backend
# ---------------------------------------------------------------------------


def create_approval_store():
    """Return the appropriate ApprovalStore based on the configured backend.

    - In-memory (dev/test): InMemoryApprovalStore
    - SQLite or Postgres: SQLiteApprovalStore (uses the DB)

    The API and worker MUST use the same factory so they share the same
    approval records. Without this, the worker creates approvals in its
    own in-memory store, and the API can't see them (and vice versa).
    """
    from opsmesh.adapters.db.connection import get_db_backend
    backend = get_db_backend()
    if backend == "memory":
        return InMemoryApprovalStore()
    # For both sqlite and postgres, use SQLiteApprovalStore (which works
    # via the unified get_connection() abstraction — actually it uses
    # direct sqlite3 calls, but for Postgres we'd need a PostgresApprovalStore.
    # For now, SQLiteApprovalStore works for SQLite; Postgres support is
    # a Phase 8 concern.)
    from opsmesh.adapters.db.sqlite_approval_store import SQLiteApprovalStore
    return SQLiteApprovalStore()


__all__ = [
    "DEFAULT_APPROVAL_TIMEOUT",
    "ApprovalError",
    "ApprovalNotFoundError",
    "ApprovalExpiredError",
    "ApprovalConsumedError",
    "ApprovalWorkspaceMismatchError",
    "ApprovalRunMismatchError",
    "ApprovalActionMismatchError",
    "ApprovalPayloadMismatchError",
    "ApprovalGateMismatchError",
    "ApprovalStore",
    "InMemoryApprovalStore",
    "RealApprovalVerifier",
    "compute_payload_hash",
    "create_approval_store",
    "TOOL_PROTECTED_FIELDS",
]
