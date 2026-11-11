"""
Tests for opsmesh.approval_gate — Gate 1 + Gate 2 approval lifecycle.

Covers:
  - Gate 1 request: creates approval, transitions to AWAITING_EVIDENCE_REVIEW
  - Gate 2 request: creates approval, transitions to AWAITING_APPROVAL
  - Gate 1 decide: proceed_with_limitations → DRAFTING
  - Gate 1 decide: request_more_info → EVIDENCE_PROVIDED
  - Gate 1 decide: stop → STOPPED_BY_USER
  - Gate 2 decide: approve → SENDING
  - Gate 2 decide: reject → REJECTED
  - Gate 2 decide: request_changes → DRAFTING
  - Duplicate decision → AlreadyResolvedError (optimistic concurrency)
  - Invalid decision for gate kind → InvalidDecisionError
  - Cross-workspace decision → rejected
  - Nonexistent approval → rejected
  - resume() before decide() → error
  - resume() marks approval CONSUMED (single-use)
  - Notification body contains dashboard link, not reply-to
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from opsmesh.adapters.notifier.mock import MockNotifier
from opsmesh.approval_gate import (
    AlreadyResolvedError,
    ApprovalGate,
    GATE1_DECISIONS,
    GATE2_DECISIONS,
    InvalidDecisionError,
)
from opsmesh.approval_store import InMemoryApprovalStore, compute_payload_hash
from opsmesh.models import (
    ApprovalKind,
    ApprovalStatus,
    LeadSource,
    RunRecord,
    utcnow,
)
from opsmesh.state_machine import State
from opsmesh.state_store import InMemoryStateStore


@pytest.fixture
def store():
    return InMemoryStateStore()


@pytest.fixture
def approval_store():
    return InMemoryApprovalStore()


@pytest.fixture
def notifier():
    return MockNotifier()


@pytest.fixture
def gate(store, approval_store, notifier):
    return ApprovalGate(
        store=store,
        approval_store=approval_store,
        notifier=notifier,
    )


@pytest.fixture
async def run(store):
    """A run at RESEARCHING state, saved to the store (async fixture)."""
    run = RunRecord(
        workspace_id="ws_1",
        lead_id="lead_1",
        state=State.RESEARCHING,
    )
    await store.save_checkpoint(run)
    return run


@pytest.fixture
def make_run_at_state(store):
    """Factory: create a run at any state. The factory itself is sync,
    but the test must save it to the store asynchronously."""

    def _make(state: State) -> RunRecord:
        return RunRecord(
            workspace_id="ws_1",
            lead_id="lead_1",
            state=state,
        )
    return _make


# ---------------------------------------------------------------------------
# 1. Gate 1 request
# ---------------------------------------------------------------------------


class TestGate1Request:
    @pytest.mark.asyncio
    async def test_request_creates_approval(self, gate, store, run):
        """request() creates an approval record and transitions to AWAITING_EVIDENCE_REVIEW."""
        payload = {"missing_info": ["company_website"]}
        ph = compute_payload_hash("evidence_review", payload)

        approval_id = await gate.request(
            workspace_id="ws_1",
            run_id=run.run_id,
            kind=ApprovalKind.EVIDENCE_REVIEW,
            action="evidence_review",
            payload=payload,
            payload_hash=ph,
            run_state=State.AWAITING_EVIDENCE_REVIEW,
        )
        assert approval_id.startswith("ap_")

        # Verify the run is now paused
        loaded = await store.load_latest(run.run_id, "ws_1")
        assert loaded.state == State.AWAITING_EVIDENCE_REVIEW
        assert loaded.pending_approval_id == approval_id
        assert loaded.is_paused()

    @pytest.mark.asyncio
    async def test_request_notifies_operator(self, gate, notifier, run):
        """request() sends a notification to the workspace owner."""
        payload = {"missing_info": ["company_website"]}
        ph = compute_payload_hash("evidence_review", payload)

        await gate.request(
            workspace_id="ws_1",
            run_id=run.run_id,
            kind=ApprovalKind.EVIDENCE_REVIEW,
            action="evidence_review",
            payload=payload,
            payload_hash=ph,
            run_state=State.AWAITING_EVIDENCE_REVIEW,
        )
        assert len(notifier.sent) == 1
        assert notifier.sent[0]["kind"] == "approval_requested"

    @pytest.mark.asyncio
    async def test_notification_contains_dashboard_link_not_reply(self, gate, notifier, run):
        """CRITICAL (Correction 5): notification contains a dashboard link,
        NOT a reply-to-approve mechanism."""
        payload = {"missing_info": ["company_website"]}
        ph = compute_payload_hash("evidence_review", payload)

        await gate.request(
            workspace_id="ws_1",
            run_id=run.run_id,
            kind=ApprovalKind.EVIDENCE_REVIEW,
            action="evidence_review",
            payload=payload,
            payload_hash=ph,
            run_state=State.AWAITING_EVIDENCE_REVIEW,
        )
        body = notifier.sent[0]["body"]
        assert "dashboard" in body.lower()
        assert "Do NOT reply" in body


# ---------------------------------------------------------------------------
# 2. Gate 1 decide
# ---------------------------------------------------------------------------


class TestGate1Decide:
    @pytest.mark.asyncio
    async def test_proceed_with_limitations(self, gate, approval_store, run):
        """proceed_with_limitations → resume to DRAFTING."""
        payload = {"missing_info": []}
        ph = compute_payload_hash("evidence_review", payload)

        approval_id = await gate.request(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EVIDENCE_REVIEW, action="evidence_review",
            payload=payload, payload_hash=ph,
            run_state=State.AWAITING_EVIDENCE_REVIEW,
        )
        await gate.decide(approval_id, "ws_1", "proceed_with_limitations", "user_1")
        run_result = await gate.resume(approval_id, "ws_1")

        assert run_result.state == State.DRAFTING

    @pytest.mark.asyncio
    async def test_request_more_info(self, gate, approval_store, run):
        """request_more_info → resume to EVIDENCE_PROVIDED."""
        payload = {"missing_info": ["company_website"]}
        ph = compute_payload_hash("evidence_review", payload)

        approval_id = await gate.request(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EVIDENCE_REVIEW, action="evidence_review",
            payload=payload, payload_hash=ph,
            run_state=State.AWAITING_EVIDENCE_REVIEW,
        )
        await gate.decide(approval_id, "ws_1", "request_more_info", "user_1")
        run_result = await gate.resume(approval_id, "ws_1")

        assert run_result.state == State.EVIDENCE_PROVIDED

    @pytest.mark.asyncio
    async def test_stop(self, gate, approval_store, run):
        """stop → resume to STOPPED_BY_USER (terminal)."""
        payload = {"missing_info": []}
        ph = compute_payload_hash("evidence_review", payload)

        approval_id = await gate.request(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EVIDENCE_REVIEW, action="evidence_review",
            payload=payload, payload_hash=ph,
            run_state=State.AWAITING_EVIDENCE_REVIEW,
        )
        await gate.decide(approval_id, "ws_1", "stop", "user_1")
        run_result = await gate.resume(approval_id, "ws_1")

        assert run_result.state == State.STOPPED_BY_USER
        assert run_result.is_terminal()


# ---------------------------------------------------------------------------
# 3. Gate 2 decide
# ---------------------------------------------------------------------------


class TestGate2Decide:
    @pytest.mark.asyncio
    async def test_approve(self, gate, store, make_run_at_state):
        """approve → resume to SENDING."""
        run = make_run_at_state(State.CRITIC)
        await store.save_checkpoint(run)

        payload = {"to": "r@acme.com", "subject": "Hi", "body": "Hello"}
        ph = compute_payload_hash("send_email", payload, ["to", "from_", "subject", "body"])

        approval_id = await gate.request(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EMAIL_SEND, action="send_email",
            payload=payload, payload_hash=ph,
            run_state=State.AWAITING_APPROVAL,
        )
        await gate.decide(approval_id, "ws_1", "approve", "user_1")
        run_result = await gate.resume(approval_id, "ws_1")

        assert run_result.state == State.SENDING

    @pytest.mark.asyncio
    async def test_reject(self, gate, store, make_run_at_state):
        """reject → resume to REJECTED (terminal, no email sent)."""
        run = make_run_at_state(State.CRITIC)
        await store.save_checkpoint(run)

        payload = {"to": "r@acme.com", "subject": "Hi", "body": "Hello"}
        ph = compute_payload_hash("send_email", payload, ["to", "from_", "subject", "body"])

        approval_id = await gate.request(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EMAIL_SEND, action="send_email",
            payload=payload, payload_hash=ph,
            run_state=State.AWAITING_APPROVAL,
        )
        await gate.decide(approval_id, "ws_1", "reject", "user_1")
        run_result = await gate.resume(approval_id, "ws_1")

        assert run_result.state == State.REJECTED
        assert run_result.is_terminal()

    @pytest.mark.asyncio
    async def test_request_changes(self, gate, store, make_run_at_state):
        """request_changes → resume to DRAFTING with feedback."""
        run = make_run_at_state(State.CRITIC)
        await store.save_checkpoint(run)

        payload = {"to": "r@acme.com", "subject": "Hi", "body": "Hello"}
        ph = compute_payload_hash("send_email", payload, ["to", "from_", "subject", "body"])

        approval_id = await gate.request(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EMAIL_SEND, action="send_email",
            payload=payload, payload_hash=ph,
            run_state=State.AWAITING_APPROVAL,
        )
        await gate.decide(
            approval_id, "ws_1", "request_changes", "user_1",
            feedback="Make the opening more personal.",
        )
        run_result = await gate.resume(approval_id, "ws_1")

        assert run_result.state == State.DRAFTING
        assert run_result.critic_feedback == "Make the opening more personal."


# ---------------------------------------------------------------------------
# 4. Duplicate decision (optimistic concurrency)
# ---------------------------------------------------------------------------


class TestDuplicateDecision:
    @pytest.mark.asyncio
    async def test_duplicate_decision_rejected(self, gate, run):
        """Second decide() on the same approval → AlreadyResolvedError."""
        payload = {"missing_info": []}
        ph = compute_payload_hash("evidence_review", payload)

        approval_id = await gate.request(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EVIDENCE_REVIEW, action="evidence_review",
            payload=payload, payload_hash=ph,
            run_state=State.AWAITING_EVIDENCE_REVIEW,
        )
        # First decision succeeds
        await gate.decide(approval_id, "ws_1", "stop", "user_1")
        # Second decision → AlreadyResolvedError
        with pytest.raises(AlreadyResolvedError):
            await gate.decide(approval_id, "ws_1", "proceed_with_limitations", "user_2")

    @pytest.mark.asyncio
    async def test_concurrent_decide_only_one_wins(self, gate, run):
        """FIX 1: Two concurrent decide() calls — only the first wins.

        The atomic CAS in the ApprovalStore guarantees that two simultaneous
        decisions cannot both succeed. The second gets AlreadyResolvedError."""
        import asyncio
        payload = {"missing_info": []}
        ph = compute_payload_hash("evidence_review", payload)

        approval_id = await gate.request(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EVIDENCE_REVIEW, action="evidence_review",
            payload=payload, payload_hash=ph,
            run_state=State.AWAITING_EVIDENCE_REVIEW,
        )

        # Launch two concurrent decide() calls
        results = await asyncio.gather(
            gate.decide(approval_id, "ws_1", "stop", "user_1"),
            gate.decide(approval_id, "ws_1", "proceed_with_limitations", "user_2"),
            return_exceptions=True,
        )

        # One should succeed, the other should raise AlreadyResolvedError
        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, AlreadyResolvedError)]
        assert len(successes) == 1
        assert len(failures) == 1


# ---------------------------------------------------------------------------
# 5. Invalid decision for gate kind
# ---------------------------------------------------------------------------


class TestInvalidDecision:
    @pytest.mark.asyncio
    async def test_gate2_decision_on_gate1_rejected(self, gate, run):
        """"approve" is not valid for Gate 1 (evidence_review)."""
        payload = {"missing_info": []}
        ph = compute_payload_hash("evidence_review", payload)

        approval_id = await gate.request(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EVIDENCE_REVIEW, action="evidence_review",
            payload=payload, payload_hash=ph,
            run_state=State.AWAITING_EVIDENCE_REVIEW,
        )
        with pytest.raises(InvalidDecisionError):
            await gate.decide(approval_id, "ws_1", "approve", "user_1")

    @pytest.mark.asyncio
    async def test_gate1_decision_on_gate2_rejected(self, gate, store, make_run_at_state):
        """"stop" is not valid for Gate 2 (email_send)."""
        run = make_run_at_state(State.CRITIC)
        await store.save_checkpoint(run)

        payload = {"to": "r@acme.com", "subject": "Hi", "body": "Hello"}
        ph = compute_payload_hash("send_email", payload, ["to", "from_", "subject", "body"])

        approval_id = await gate.request(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EMAIL_SEND, action="send_email",
            payload=payload, payload_hash=ph,
            run_state=State.AWAITING_APPROVAL,
        )
        with pytest.raises(InvalidDecisionError):
            await gate.decide(approval_id, "ws_1", "stop", "user_1")

    def test_gate1_decisions_set(self):
        assert GATE1_DECISIONS == {"proceed_with_limitations", "request_more_info", "stop"}

    def test_gate2_decisions_set(self):
        assert GATE2_DECISIONS == {"approve", "reject", "request_changes"}


# ---------------------------------------------------------------------------
# 6. Cross-workspace decision
# ---------------------------------------------------------------------------


class TestCrossWorkspaceDecision:
    @pytest.mark.asyncio
    async def test_cross_workspace_decide_rejected(self, gate, run):
        """Decision from workspace B on workspace A's approval → rejected."""
        payload = {"missing_info": []}
        ph = compute_payload_hash("evidence_review", payload)

        approval_id = await gate.request(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EVIDENCE_REVIEW, action="evidence_review",
            payload=payload, payload_hash=ph,
            run_state=State.AWAITING_EVIDENCE_REVIEW,
        )
        from opsmesh.approval_gate import ApprovalGateError
        with pytest.raises(ApprovalGateError) as exc_info:
            await gate.decide(approval_id, "ws_2", "stop", "user_1")
        assert "workspace" in exc_info.value.code


# ---------------------------------------------------------------------------
# 7. resume() before decide()
# ---------------------------------------------------------------------------


class TestResumeBeforeDecide:
    @pytest.mark.asyncio
    async def test_resume_before_decide_raises(self, gate, run):
        """resume() on a PENDING approval (no decision yet) → error."""
        payload = {"missing_info": []}
        ph = compute_payload_hash("evidence_review", payload)

        approval_id = await gate.request(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EVIDENCE_REVIEW, action="evidence_review",
            payload=payload, payload_hash=ph,
            run_state=State.AWAITING_EVIDENCE_REVIEW,
        )
        from opsmesh.approval_gate import ApprovalGateError
        with pytest.raises(ApprovalGateError) as exc_info:
            await gate.resume(approval_id, "ws_1")
        assert "not_decided" in exc_info.value.code


# ---------------------------------------------------------------------------
# 8. resume() marks CONSUMED (single-use)
# ---------------------------------------------------------------------------


class TestResumeConsumesApproval:
    @pytest.mark.asyncio
    async def test_resume_marks_consumed(self, gate, approval_store, run):
        """After resume(), the approval is CONSUMED (single-use)."""
        payload = {"missing_info": []}
        ph = compute_payload_hash("evidence_review", payload)

        approval_id = await gate.request(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EVIDENCE_REVIEW, action="evidence_review",
            payload=payload, payload_hash=ph,
            run_state=State.AWAITING_EVIDENCE_REVIEW,
        )
        await gate.decide(approval_id, "ws_1", "proceed_with_limitations", "user_1")
        await gate.resume(approval_id, "ws_1")

        record = approval_store.get(approval_id)
        assert record.status == ApprovalStatus.CONSUMED

    @pytest.mark.asyncio
    async def test_consumed_approval_cannot_be_reused(self, gate, approval_store, run):
        """After CONSUMED, the approval cannot be decided again."""
        payload = {"missing_info": []}
        ph = compute_payload_hash("evidence_review", payload)

        approval_id = await gate.request(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EVIDENCE_REVIEW, action="evidence_review",
            payload=payload, payload_hash=ph,
            run_state=State.AWAITING_EVIDENCE_REVIEW,
        )
        await gate.decide(approval_id, "ws_1", "proceed_with_limitations", "user_1")
        await gate.resume(approval_id, "ws_1")

        with pytest.raises(AlreadyResolvedError):
            await gate.decide(approval_id, "ws_1", "stop", "user_2")
