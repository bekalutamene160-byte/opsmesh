"""
OPSMESH ApprovalGate — human-in-the-loop approval (Gate 1 + Gate 2).

ARCHITECTURE (course Ch 12 + Correction 5):
  Two distinct gates, each with its own approval kind:

  GATE 1 (evidence_review):
    - Triggered when research returns sufficient_for_qualification=False
      OR qualification returns NEEDS_REVIEW.
    - Three decisions:
        proceed_with_limitations → transition to DRAFTING (with documented limitation)
        request_more_info         → transition to EVIDENCE_PROVIDED (human supplies evidence)
        stop                       → transition to STOPPED_BY_USER (terminal)
    - Gate 1 NEVER authorizes sending (Correction 5).

  GATE 2 (email_send):
    - Triggered before sending an external customer email.
    - Three decisions:
        approve         → transition to SENDING (email is sent)
        reject          → transition to REJECTED (terminal, no email sent)
        request_changes → transition to DRAFTING (retry with feedback)
    - Approval is bound to the EXACT payload (recipient, subject, body).
    - A material change invalidates the approval (StaleApprovalError).

OPTIMISTIC CONCURRENCY:
  The decide() method uses atomic compare-and-swap: only the first
  decision wins. Duplicate "Approve" clicks → second returns 409 AlreadyResolved.

24H TIMEOUT:
  Pending approvals expire after 24 hours (configurable per workspace).
  The approval_timeout reconciler finds expired approvals and transitions
  the run to APPROVAL_EXPIRED (terminal).

USAGE:
  gate = ApprovalGate(store=state_store, approval_store=approval_store,
                       notifier=notifier)

  # Gate 1: supervisor pauses
  approval_id = await gate.request(
      workspace_id="ws_1", run_id="run_1",
      kind=ApprovalKind.EVIDENCE_REVIEW,
      action="evidence_review",
      payload={"missing_info": ["company_website"]},
      payload_hash="...",
      run_state=State.AWAITING_EVIDENCE_REVIEW,
  )

  # Human decides (via API endpoint)
  await gate.decide(
      approval_id=approval_id,
      workspace_id="ws_1",
      decision="proceed_with_limitations",
      decided_by="user_123",
      feedback="Proceeding with low confidence on industry.",
  )

  # Supervisor resumes
  run = await gate.resume(approval_id, workspace_id="ws_1")
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Optional

from opsmesh.approval_store import (
    ApprovalConsumedError,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalStore,
    DEFAULT_APPROVAL_TIMEOUT,
    InMemoryApprovalStore,
)
from opsmesh.models import (
    ApprovalKind,
    ApprovalRecord,
    ApprovalStatus,
    RunRecord,
    utcnow,
)
from opsmesh.state_machine import State, can_transition, transition
from opsmesh.state_store import StateStore


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ApprovalGateError(Exception):
    """Base class for ApprovalGate errors."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class AlreadyResolvedError(ApprovalGateError):
    """The approval was already decided. Second decision is rejected (idempotent)."""

    def __init__(self, approval_id: str, prior_decision: str):
        super().__init__(
            "already_resolved",
            f"Approval {approval_id} was already decided as '{prior_decision}'",
        )


class InvalidDecisionError(ApprovalGateError):
    """The decision is not valid for this gate kind."""

    def __init__(self, decision: str, kind: ApprovalKind):
        super().__init__(
            "invalid_decision",
            f"Decision '{decision}' is not valid for {kind.value} approval. "
            f"Gate 1 allows: proceed_with_limitations, request_more_info, stop. "
            f"Gate 2 allows: approve, reject, request_changes.",
        )


class ApprovalGateMismatchError(ApprovalGateError):
    """Gate 1 approval cannot authorize Gate 2 actions (Correction 5)."""

    def __init__(self, approval_id: str, kind: ApprovalKind, required_kind: ApprovalKind):
        super().__init__(
            "gate_mismatch",
            f"Approval {approval_id} is a {kind.value} approval, "
            f"but a {required_kind.value} approval is required.",
        )


# ---------------------------------------------------------------------------
# Valid decisions per gate
# ---------------------------------------------------------------------------

GATE1_DECISIONS = {"proceed_with_limitations", "request_more_info", "stop"}
GATE2_DECISIONS = {"approve", "reject", "request_changes"}


# ---------------------------------------------------------------------------
# The ApprovalGate
# ---------------------------------------------------------------------------


class ApprovalGate:
    """
    The human-in-the-loop approval gate.

    Manages both Gate 1 (evidence review) and Gate 2 (email send).

    Lifecycle:
        1. request()  — supervisor pauses, creates approval record, notifies.
        2. decide()   — human makes a decision via the API.
        3. resume()   — supervisor resumes the run based on the decision.

    The gate NEVER sends emails or updates CRM directly. It only:
        - Creates/approves/rejects approval records.
        - Transitions run state.
        - Notifies the operator (via the Notifier adapter).
    """

    def __init__(
        self,
        store: StateStore,
        approval_store: ApprovalStore,
        notifier=None,  # Optional[Notifier] — Day 5 uses MockNotifier in tests
        timeout_hours: int = 24,
    ):
        self.store = store
        self.approval_store = approval_store
        self.notifier = notifier
        self.timeout = timedelta(hours=timeout_hours)

    # -------------------------------------------------------------------
    # request() — called by the supervisor when it needs to pause
    # -------------------------------------------------------------------

    async def request(
        self,
        workspace_id: str,
        run_id: str,
        kind: ApprovalKind,
        action: str,
        payload: dict,
        payload_hash: str,
        run_state: State,
        step_id: int = 0,
    ) -> str:
        """
        Pause a run and create an approval request.

        Steps:
            1. Create an approval record in the ApprovalStore.
            2. Transition the run to the paused state.
            3. Save the checkpoint (with pending_approval_id set).
            4. Notify the operator (if notifier is configured).

        Returns:
            The approval_id (used by the human to make a decision).
        """
        # 1. Create the approval record
        expires_at = utcnow() + self.timeout
        record = self.approval_store.create(
            workspace_id=workspace_id,
            run_id=run_id,
            kind=kind,
            action=action,
            payload=payload,
            payload_hash=payload_hash,
            expires_at=expires_at,
        )

        # 2. Load the run and transition to the paused state
        #    IMPORTANT: if the run is ALREADY in the target state (e.g., the
        #    supervisor already transitioned to AWAITING_EVIDENCE_REVIEW before
        #    calling this method), we skip the transition. Same-state transitions
        #    are illegal in the state machine, so we guard against them here.
        run = await self.store.load_latest(run_id, workspace_id)
        if run is None:
            raise ApprovalGateError(
                "run_not_found",
                f"Run {run_id} not found in workspace {workspace_id}",
            )

        run.pending_approval_id = record.id
        if run.state != run_state:
            # Only transition if we're not already in the target state
            run.state = transition(run.state, run_state)
        run.step_id = step_id
        await self.store.save_checkpoint(run)

        # 3. Notify the operator (best-effort; non-blocking)
        if self.notifier is not None:
            try:
                await self.notifier.send(
                    to="workspace_owner",
                    subject=f"OPSMESH approval required: {kind.value}",
                    body=self._build_notification_body(
                        kind, action, payload, record.id, expires_at,
                    ),
                    kind="approval_requested",
                )
            except Exception:
                pass  # notification is best-effort; don't block the gate

        return record.id

    # -------------------------------------------------------------------
    # decide() — called by the API when a human makes a decision
    # -------------------------------------------------------------------

    async def decide(
        self,
        approval_id: str,
        workspace_id: str,
        decision: str,
        decided_by: str,
        feedback: Optional[str] = None,
    ) -> ApprovalRecord:
        """
        Submit a human decision on an approval.

        CONCURRENCY SAFETY (Fix 1):
            Delegates to approval_store.decide() which is ATOMIC. Only the
            first caller transitions PENDING → APPROVED/REJECTED/REQUEST_CHANGES.
            Concurrent callers get AlreadyResolvedError.

        Validates:
            1. Approval exists.
            2. Belongs to the correct workspace.
            3. The decision is valid for this gate kind (Gate 1 vs Gate 2).

        The atomic CAS (PENDING → decided) is performed by the store, NOT
        by this method. This method validates the decision is legal for the
        gate kind, then delegates the atomic transition.

        Returns:
            The updated ApprovalRecord.

        Raises:
            ApprovalNotFoundError: approval doesn't exist.
            AlreadyResolvedError: approval was already decided (atomic CAS failed).
            InvalidDecisionError: decision not valid for this gate kind.
        """
        # 1. Look up the approval (read-only, for validation)
        record = self.approval_store.get(approval_id)
        if record is None:
            raise ApprovalNotFoundError(approval_id)

        # 2. Check workspace match
        if record.workspace_id != workspace_id:
            raise ApprovalGateError(
                "workspace_mismatch",
                f"Approval {approval_id} belongs to workspace {record.workspace_id}, "
                f"not {workspace_id}",
            )

        # 3. Validate the decision against the gate kind (before atomic CAS)
        if record.kind == ApprovalKind.EVIDENCE_REVIEW:
            if decision not in GATE1_DECISIONS:
                raise InvalidDecisionError(decision, record.kind)
            # Map decision to status
            if decision == "proceed_with_limitations":
                new_status = ApprovalStatus.APPROVED
            elif decision == "request_more_info":
                new_status = ApprovalStatus.REQUEST_CHANGES
            elif decision == "stop":
                new_status = ApprovalStatus.REJECTED
            else:
                raise InvalidDecisionError(decision, record.kind)
        elif record.kind == ApprovalKind.EMAIL_SEND:
            if decision not in GATE2_DECISIONS:
                raise InvalidDecisionError(decision, record.kind)
            if decision == "approve":
                new_status = ApprovalStatus.APPROVED
            elif decision == "reject":
                new_status = ApprovalStatus.REJECTED
            elif decision == "request_changes":
                new_status = ApprovalStatus.REQUEST_CHANGES
            else:
                raise InvalidDecisionError(decision, record.kind)
        else:
            raise ApprovalGateError(
                "unknown_kind",
                f"Approval {approval_id} has unknown kind: {record.kind}",
            )

        # 4. ATOMIC CAS: delegate to the store's concurrency-safe decide()
        # The store guarantees that only the first caller wins.
        # Concurrent callers get AlreadyResolvedError.
        return await self.approval_store.decide(
            approval_id=approval_id,
            workspace_id=workspace_id,
            new_status=new_status,
            decision=decision,
            decided_by=decided_by,
            feedback=feedback,
        )

    # -------------------------------------------------------------------
    # resume() — called by the supervisor to continue a paused run
    # -------------------------------------------------------------------

    async def resume(
        self,
        approval_id: str,
        workspace_id: str,
    ) -> RunRecord:
        """
        Resume a run after a human decision.

        Loads the approval record, checks its status, and transitions
        the run to the appropriate next state:

        Gate 1 decisions:
            proceed_with_limitations → DRAFTING (with limitation recorded)
            request_more_info         → EVIDENCE_PROVIDED (human will supply evidence)
            stop                       → STOPPED_BY_USER (terminal)

        Gate 2 decisions:
            approve         → SENDING (email is sent)
            reject          → REJECTED (terminal, no email sent)
            request_changes → DRAFTING (retry with feedback)

        The approval is marked CONSUMED (single-use).

        Returns:
            The updated RunRecord (reloaded from the store).
        """
        # 1. Look up the approval
        record = self.approval_store.get(approval_id)
        if record is None:
            raise ApprovalNotFoundError(approval_id)

        # 2. Check workspace match
        if record.workspace_id != workspace_id:
            raise ApprovalGateError(
                "workspace_mismatch",
                f"Approval {approval_id} belongs to workspace {record.workspace_id}",
            )

        # 3. Check if a decision has been made
        if record.status == ApprovalStatus.PENDING:
            raise ApprovalGateError(
                "not_decided",
                f"Approval {approval_id} is still PENDING — no decision has been made",
            )

        # 4. Load the run
        run = await self.store.load_latest(record.run_id, workspace_id)
        if run is None:
            raise ApprovalGateError(
                "run_not_found",
                f"Run {record.run_id} not found",
            )

        # 5. Determine the target state based on the decision
        target_state = self._get_target_state(record)

        # 6. Transition the run
        run.state = transition(run.state, target_state)
        run.pending_approval_id = None  # clear the pending approval

        # For request_changes (Gate 2), set critic_feedback
        if record.decision == "request_changes":
            run.critic_feedback = record.decision_feedback or "Human requested changes."

        # For proceed_with_limitations (Gate 1), record the limitation
        if record.decision == "proceed_with_limitations":
            # The limitation is recorded in the qualification as NEEDS_REVIEW
            # (the Draft specialist will include it in the draft prompt)
            pass

        # 7. Mark the approval as CONSUMED (single-use)
        record.status = ApprovalStatus.CONSUMED

        # 8. Save the checkpoint
        await self.store.save_checkpoint(run)

        return run

    def _get_target_state(self, record: ApprovalRecord) -> State:
        """Determine the target state based on the approval's decision."""
        if record.kind == ApprovalKind.EVIDENCE_REVIEW:
            if record.decision == "proceed_with_limitations":
                return State.DRAFTING
            elif record.decision == "request_more_info":
                return State.EVIDENCE_PROVIDED
            elif record.decision == "stop":
                return State.STOPPED_BY_USER
        elif record.kind == ApprovalKind.EMAIL_SEND:
            if record.decision == "approve":
                return State.SENDING
            elif record.decision == "reject":
                return State.REJECTED
            elif record.decision == "request_changes":
                return State.DRAFTING

        raise ApprovalGateError(
            "unknown_decision",
            f"Cannot determine target state for decision '{record.decision}' "
            f"on {record.kind.value} approval",
        )

    def _build_notification_body(
        self,
        kind: ApprovalKind,
        action: str,
        payload: dict,
        approval_id: str,
        expires_at,
    ) -> str:
        """Build the notification body for an approval request.

        CRITICAL (Correction 5): the notification contains an authenticated
        dashboard link, NOT a reply-to-approve mechanism. Approval NEVER
        happens via email reply.
        """
        return (
            f"OPSMESH Approval Required\n\n"
            f"Gate: {kind.value}\n"
            f"Action: {action}\n"
            f"Approval ID: {approval_id}\n"
            f"Expires: {expires_at.isoformat()}\n\n"
            f"Payload summary: {str(payload)[:500]}\n\n"
            f"To approve or reject, visit the OPSMESH dashboard:\n"
            f"https://dashboard.opsmesh.dev/approvals/{approval_id}\n\n"
            f"Do NOT reply to this email — approval must happen via the "
            f"authenticated dashboard."
        )


__all__ = [
    "ApprovalGate",
    "ApprovalGateError",
    "AlreadyResolvedError",
    "InvalidDecisionError",
    "ApprovalGateMismatchError",
    "GATE1_DECISIONS",
    "GATE2_DECISIONS",
]
