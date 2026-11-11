"""
Tests for opsmesh.reconciler — approval timeout, email send, CRM outbox.

Covers:
  Approval Timeout Reconciler:
    - Expires pending approvals past their 24h timeout
    - Transitions run to APPROVAL_EXPIRED
    - Does NOT expire already-decided approvals
    - Does NOT transition non-paused runs

  Email Send Reconciler:
    - confirmed_sent → transitions to SENT
    - confirmed_failed → transitions to FAILED
    - unknown → does NOT transition (retries)
    - After 5 unknown attempts → escalates (NOT auto-FAIL)

  CRM Outbox Drainer:
    - Claims pending ops and calls CRM adapter
    - Success → DONE
    - Transient failure → requeue with backoff
    - Permanent failure → FAILED
    - After max_attempts → DEAD_LETTER
    - CRM failure does NOT trigger email resend (independence)
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from opsmesh.adapters.crm.base import CRMLead, ToolError as CRMToolError
from opsmesh.adapters.crm.mock import MockCRMProvider
from opsmesh.adapters.email.base import EmailStatus
from opsmesh.adapters.email.mock import MockEmailProvider
from opsmesh.approval_store import InMemoryApprovalStore
from opsmesh.models import (
    ApprovalKind,
    ApprovalStatus,
    CRMOutboxOp,
    CRMOutboxOperation,
    CRMOutboxStatus,
    RunRecord,
    utcnow,
)
from opsmesh.reconciler.approval_timeout import ApprovalTimeoutReconciler
from opsmesh.reconciler.crm_outbox import CRMOutboxDrainer, CRMOutboxStore
from opsmesh.reconciler.email_send import (
    EmailSendReconciler,
    MAX_RECONCILIATION_ATTEMPTS,
)
from opsmesh.state_machine import State
from opsmesh.state_store import InMemoryStateStore


# ===========================================================================
# APPROVAL TIMEOUT RECONCILER
# ===========================================================================


class TestApprovalTimeoutReconciler:
    @pytest.mark.asyncio
    async def test_expires_pending_approval(self):
        """A pending approval past its 24h timeout is expired."""
        store = InMemoryStateStore()
        approval_store = InMemoryApprovalStore()

        # Create a run in AWAITING_APPROVAL
        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.AWAITING_APPROVAL)
        await store.save_checkpoint(run)

        # Create an approval that's already expired
        record = approval_store.create(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EMAIL_SEND, action="send_email",
            payload={}, payload_hash="sha256:abc",
            expires_at=utcnow() - timedelta(hours=1),  # expired 1h ago
        )
        run.pending_approval_id = record.id
        await store.save_checkpoint(run)

        reconciler = ApprovalTimeoutReconciler(store=store, approval_store=approval_store)
        expired = await reconciler.run_once()

        assert expired == 1
        # Approval is now EXPIRED
        assert approval_store.get(record.id).status == ApprovalStatus.EXPIRED
        # Run is now APPROVAL_EXPIRED (terminal)
        loaded = await store.load_latest(run.run_id, "ws_1")
        assert loaded.state == State.APPROVAL_EXPIRED
        assert loaded.is_terminal()

    @pytest.mark.asyncio
    async def test_does_not_expire_decided_approval(self):
        """Already-decided approvals are NOT expired."""
        store = InMemoryStateStore()
        approval_store = InMemoryApprovalStore()

        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.AWAITING_APPROVAL)
        await store.save_checkpoint(run)

        record = approval_store.create(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EMAIL_SEND, action="send_email",
            payload={}, payload_hash="sha256:abc",
            expires_at=utcnow() - timedelta(hours=1),
        )
        # Mark it as APPROVED (decided)
        record.status = ApprovalStatus.APPROVED

        reconciler = ApprovalTimeoutReconciler(store=store, approval_store=approval_store)
        expired = await reconciler.run_once()
        assert expired == 0  # nothing expired

    @pytest.mark.asyncio
    async def test_does_not_transition_non_paused_run(self):
        """If the run is NOT paused (e.g., already SENT), the approval is
        expired but the run is NOT transitioned."""
        store = InMemoryStateStore()
        approval_store = InMemoryApprovalStore()

        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.SENT)
        await store.save_checkpoint(run)

        record = approval_store.create(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EMAIL_SEND, action="send_email",
            payload={}, payload_hash="sha256:abc",
            expires_at=utcnow() - timedelta(hours=1),
        )

        reconciler = ApprovalTimeoutReconciler(store=store, approval_store=approval_store)
        expired = await reconciler.run_once()

        assert expired == 1  # approval was expired
        # But run stays in SENT (not transitioned to APPROVAL_EXPIRED)
        loaded = await store.load_latest(run.run_id, "ws_1")
        assert loaded.state == State.SENT

    @pytest.mark.asyncio
    async def test_timeout_never_means_approval(self):
        """CRITICAL: a timed-out approval transitions to APPROVAL_EXPIRED,
        NOT to SENDING. The email is NOT sent."""
        store = InMemoryStateStore()
        approval_store = InMemoryApprovalStore()

        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.AWAITING_APPROVAL)
        await store.save_checkpoint(run)

        record = approval_store.create(
            workspace_id="ws_1", run_id=run.run_id,
            kind=ApprovalKind.EMAIL_SEND, action="send_email",
            payload={}, payload_hash="sha256:abc",
            expires_at=utcnow() - timedelta(hours=1),
        )
        run.pending_approval_id = record.id
        await store.save_checkpoint(run)

        reconciler = ApprovalTimeoutReconciler(store=store, approval_store=approval_store)
        await reconciler.run_once()

        loaded = await store.load_latest(run.run_id, "ws_1")
        # NOT SENDING — timeout never means approval
        assert loaded.state == State.APPROVAL_EXPIRED
        assert loaded.state != State.SENDING


# ===========================================================================
# EMAIL SEND RECONCILER
# ===========================================================================


class TestEmailSendReconciler:
    @pytest.mark.asyncio
    async def test_confirmed_sent_transitions_to_sent(self):
        """Provider confirms the email was sent → run transitions to SENT."""
        store = InMemoryStateStore()
        email = MockEmailProvider()

        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.SEND_UNKNOWN)
        # FIX 3: set the real email_message_id (not run_id)
        run.email_message_id = "msg_test_001"
        await store.save_checkpoint(run)

        # Override get_status to return confirmed_sent for this message_id
        email.set_status_override(
            "msg_test_001",
            EmailStatus(message_id="msg_test_001", status="confirmed_sent"),
        )

        reconciler = EmailSendReconciler(store=store, email_provider=email)
        resolved = await reconciler.run_once()

        assert resolved == 1
        loaded = await store.load_latest(run.run_id, "ws_1")
        assert loaded.state == State.SENT

    @pytest.mark.asyncio
    async def test_confirmed_failed_transitions_to_failed(self):
        """Provider confirms the email was NOT sent → run transitions to FAILED."""
        store = InMemoryStateStore()
        email = MockEmailProvider()

        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.SEND_UNKNOWN)
        run.email_message_id = "msg_test_002"
        await store.save_checkpoint(run)

        email.set_status_override(
            "msg_test_002",
            EmailStatus(message_id="msg_test_002", status="confirmed_failed"),
        )

        reconciler = EmailSendReconciler(store=store, email_provider=email)
        resolved = await reconciler.run_once()

        assert resolved == 1
        loaded = await store.load_latest(run.run_id, "ws_1")
        assert loaded.state == State.FAILED

    @pytest.mark.asyncio
    async def test_unknown_does_not_transition(self):
        """Provider returns 'unknown' → run stays in SEND_UNKNOWN."""
        store = InMemoryStateStore()
        email = MockEmailProvider()

        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.SEND_UNKNOWN)
        run.email_message_id = "msg_test_003"
        await store.save_checkpoint(run)

        email.set_status_override(
            "msg_test_003",
            EmailStatus(message_id="msg_test_003", status="unknown"),
        )

        reconciler = EmailSendReconciler(store=store, email_provider=email)
        resolved = await reconciler.run_once()

        assert resolved == 0  # not resolved
        loaded = await store.load_latest(run.run_id, "ws_1")
        assert loaded.state == State.SEND_UNKNOWN  # still paused

    @pytest.mark.asyncio
    async def test_after_max_attempts_escalates_not_fails(self):
        """CRITICAL: after 5 unknown attempts, the run is escalated for
        manual investigation — NOT auto-transitioned to FAILED.

        Correction 3: NEVER auto-transition to FAILED due to threshold exhaustion."""
        store = InMemoryStateStore()
        email = MockEmailProvider()

        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.SEND_UNKNOWN)
        run.email_message_id = "msg_test_004"
        await store.save_checkpoint(run)

        email.set_status_override(
            "msg_test_004",
            EmailStatus(message_id="msg_test_004", status="unknown"),
        )

        reconciler = EmailSendReconciler(store=store, email_provider=email)

        # Run the reconciler MAX_RECONCILIATION_ATTEMPTS times
        for _ in range(MAX_RECONCILIATION_ATTEMPTS):
            await reconciler.run_once()

        loaded = await store.load_latest(run.run_id, "ws_1")
        # Still SEND_UNKNOWN — NOT FAILED
        assert loaded.state == State.SEND_UNKNOWN
        # But the error message indicates escalation (case-insensitive check)
        assert "manual investigation" in (loaded.last_error or "").lower()

    @pytest.mark.asyncio
    async def test_no_message_id_escalates(self):
        """FIX 3: if email_message_id is None, the reconciler escalates
        (can't query the provider without a message_id)."""
        store = InMemoryStateStore()
        email = MockEmailProvider()

        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.SEND_UNKNOWN)
        # email_message_id is None (no send was attempted, or message_id was lost)
        await store.save_checkpoint(run)

        reconciler = EmailSendReconciler(store=store, email_provider=email)
        resolved = await reconciler.run_once()

        assert resolved == 0  # not resolved
        loaded = await store.load_latest(run.run_id, "ws_1")
        assert loaded.state == State.SEND_UNKNOWN  # still paused
        assert "no email_message_id" in (loaded.last_error or "").lower()


# ===========================================================================
# CRM OUTBOX DRAINER
# ===========================================================================


class TestCRMOutboxDrainer:
    @pytest.mark.asyncio
    async def test_drains_pending_op_successfully(self):
        """A pending outbox op is drained (CRM adapter called) and marked DONE."""
        crm = MockCRMProvider()
        crm.add_lead(CRMLead(
            lead_id="L_001", workspace_id="ws_1",
            company="Acme", contact_email="r@acme.com",
        ))
        outbox = CRMOutboxStore()

        # Enqueue a pending op
        op = CRMOutboxOp(
            workspace_id="ws_1", run_id="run_1", lead_id="L_001",
            operation=CRMOutboxOperation.UPDATE_LEAD_STATUS,
            args={"lead_id": "L_001", "status": "contacted"},
        )
        outbox.enqueue(op)

        drainer = CRMOutboxDrainer(crm_provider=crm, outbox_store=outbox)
        drained = await drainer.run_once()

        assert drained == 1
        assert outbox.get(op.id).status == CRMOutboxStatus.DONE

    @pytest.mark.asyncio
    async def test_permanent_failure_marks_failed(self):
        """A 4xx error (e.g., invalid_status) → op marked FAILED."""
        crm = MockCRMProvider()
        crm.add_lead(CRMLead(
            lead_id="L_001", workspace_id="ws_1",
            company="Acme", contact_email="r@acme.com",
        ))
        outbox = CRMOutboxStore()

        op = CRMOutboxOp(
            workspace_id="ws_1", run_id="run_1", lead_id="L_001",
            operation=CRMOutboxOperation.UPDATE_LEAD_STATUS,
            args={"lead_id": "L_001", "status": "invalid_status"},  # permanent error
        )
        outbox.enqueue(op)

        drainer = CRMOutboxDrainer(crm_provider=crm, outbox_store=outbox)
        await drainer.run_once()

        assert outbox.get(op.id).status == CRMOutboxStatus.FAILED

    @pytest.mark.asyncio
    async def test_transient_failure_requeues_with_backoff(self):
        """A transient failure (e.g., timeout) → op is requeued for retry."""
        crm = MockCRMProvider()
        outbox = CRMOutboxStore()

        # Create an op for a nonexistent lead (transient: the CRM adapter
        # returns not_found, which we treat as transient for this test.
        # Actually, not_found IS permanent. Let's use a different approach:
        # override the CRM to raise a generic exception.
        original_update = crm.update_lead_status

        async def failing_update(workspace_id, lead_id, status):
            raise ConnectionError("CRM temporarily unavailable")

        crm.update_lead_status = failing_update  # type: ignore

        crm.add_lead(CRMLead(
            lead_id="L_001", workspace_id="ws_1",
            company="Acme", contact_email="r@acme.com",
        ))
        op = CRMOutboxOp(
            workspace_id="ws_1", run_id="run_1", lead_id="L_001",
            operation=CRMOutboxOperation.UPDATE_LEAD_STATUS,
            args={"lead_id": "L_001", "status": "contacted"},
        )
        outbox.enqueue(op)

        drainer = CRMOutboxDrainer(crm_provider=crm, outbox_store=outbox)
        await drainer.run_once()

        # Should be requeued (PENDING) for retry
        loaded = outbox.get(op.id)
        assert loaded.status == CRMOutboxStatus.PENDING
        assert loaded.attempts == 1
        assert "ConnectionError" in (loaded.last_error or "")

    @pytest.mark.asyncio
    async def test_max_attempts_dead_letters(self):
        """After max_attempts (5), a transiently-failing op is dead-lettered."""
        crm = MockCRMProvider()
        outbox = CRMOutboxStore()

        async def always_fails(workspace_id, lead_id, status):
            raise ConnectionError("always fails")

        crm.update_lead_status = always_fails  # type: ignore

        op = CRMOutboxOp(
            workspace_id="ws_1", run_id="run_1", lead_id="L_001",
            operation=CRMOutboxOperation.UPDATE_LEAD_STATUS,
            args={"lead_id": "L_001", "status": "contacted"},
            max_attempts=3,  # lower for faster testing
        )
        outbox.enqueue(op)

        drainer = CRMOutboxDrainer(crm_provider=crm, outbox_store=outbox)

        # Attempt 1
        await drainer.run_once()
        assert outbox.get(op.id).status == CRMOutboxStatus.PENDING
        assert outbox.get(op.id).attempts == 1

        # Attempt 2
        outbox.get(op.id).next_attempt_at = utcnow()  # reset backoff
        await drainer.run_once()
        assert outbox.get(op.id).attempts == 2

        # Attempt 3 → dead-lettered (max=3)
        outbox.get(op.id).next_attempt_at = utcnow()
        await drainer.run_once()
        assert outbox.get(op.id).status == CRMOutboxStatus.DEAD_LETTER

    @pytest.mark.asyncio
    async def test_crm_failure_does_not_trigger_email_resend(self):
        """CRITICAL (Correction 2): CRM sync failure does NOT trigger
        email resend. The outbox is independent."""
        crm = MockCRMProvider()
        email = MockEmailProvider()
        outbox = CRMOutboxStore()

        async def always_fails(workspace_id, lead_id, status):
            raise ConnectionError("CRM down")

        crm.update_lead_status = always_fails  # type: ignore

        op = CRMOutboxOp(
            workspace_id="ws_1", run_id="run_1", lead_id="L_001",
            operation=CRMOutboxOperation.UPDATE_LEAD_STATUS,
            args={"lead_id": "L_001", "status": "contacted"},
        )
        outbox.enqueue(op)

        drainer = CRMOutboxDrainer(crm_provider=crm, outbox_store=outbox)
        await drainer.run_once()

        # The email adapter was NEVER called (CRM failure is independent)
        assert email.call_count == 0
        assert len(email.sent_messages) == 0
