"""Stage B Phase 2 (P0-1) regression tests: ToolGateway enforcement at the send site.

These tests prove the 7-check policy layer is actually wired into the
SendingSpecialist + CRMUpdatingSpecialist execution path — not just defined.

The four mandate tests:
  1. test_tool_gateway_enforced_on_send_email — approve a Gate 2 payload,
     modify the body before send, assert the send is rejected
     (ApprovalPayloadMismatchError → StaleApprovalError).
  2. test_send_with_unverified_approval_fails_closed — synthesize the old
     f"ap_{run_hex}" approval_id and assert the send is rejected
     (approval_not_found).
  3. test_tool_gateway_enforced_on_crm_create — assert create_lead requires a
     verified approval + workspace scope.
  4. test_tool_gateway_bypass_attempt_rejected — when the gateway is wired,
     the specialist routes through it (not the provider directly); a payload
     mismatch never reaches the provider.
"""

from __future__ import annotations

import pytest

from opsmesh.adapters.email.base import EmailMessage
from opsmesh.adapters.email.mock import MockEmailProvider
from opsmesh.approval_store import (
    InMemoryApprovalStore,
    RealApprovalVerifier,
    compute_payload_hash,
    TOOL_PROTECTED_FIELDS,
)
from opsmesh.config import Settings
from opsmesh.models import ApprovalKind, DraftResult, RunRecord
from opsmesh.security_context import AuthContext
from opsmesh.specialists import SendingSpecialist
from opsmesh.state_store import InMemoryStateStore
from opsmesh.tool_gateway import ToolGateway, GatewayError, StaleApprovalError
from opsmesh.worker import build_tool_gateway


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings_multi_user(kill_switch=False):
    """Settings with access_mode=multi_user (skips the demo recipient check)
    and email_kill_switch off so the send reaches the gateway path."""
    return Settings(
        access_mode="multi_user",
        email_kill_switch=kill_switch,
        email_provider="mock",
        workspace_sender_identity="OpsMesh <onboarding@resend.dev>",
    )


def _make_run(draft_body="Original body about Acme's growth."):
    from opsmesh.state_machine import State
    run = RunRecord(
        run_id="run_test_001",
        workspace_id="ws_test",
        lead_id="lead_abc",
        state=State.SENDING,
    )
    run.draft = DraftResult(
        subject="Quick idea for Acme",
        body=draft_body,
        recipient="founder@acme.com",
    )
    run.pending_approval_id = None
    run.step_id = 5
    return run


def _make_gateway(store, email_provider):
    """Build a ToolGateway wired to the email provider via provider_router."""
    approval_store = InMemoryApprovalStore()
    return (
        approval_store,
        build_tool_gateway(
            settings=_settings_multi_user(),
            store=store,
            approval_store=approval_store,
            email_provider=email_provider,
            crm_provider=None,
        ),
    )


def _create_gate2_approval(approval_store, run, body):
    """Create + APPROVE a Gate 2 (email_send) approval for the given payload."""
    from opsmesh.models import ApprovalStatus
    payload = {
        "to": run.draft.recipient,
        "from_": "OpsMesh <onboarding@resend.dev>",
        "subject": run.draft.subject,
        "body": body,
    }
    protected = TOOL_PROTECTED_FIELDS["send_email"]
    payload_hash = compute_payload_hash("send_email", payload, protected)
    record = approval_store.create(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        kind=ApprovalKind.EMAIL_SEND,
        action="send_email",
        payload=payload,
        payload_hash=payload_hash,
    )
    # Mark it APPROVED (human decided) so verify_and_consume can consume it.
    record.status = ApprovalStatus.APPROVED
    approval_store._approvals[record.id] = record
    return record


# ---------------------------------------------------------------------------
# 1. Gateway enforced on send_email — payload mismatch is rejected
# ---------------------------------------------------------------------------


class TestGatewayEnforcedOnSendEmail:
    @pytest.mark.asyncio
    async def test_send_with_matching_payload_succeeds(self):
        """A send whose payload matches the approved hash succeeds (approval
        consumed, provider called once)."""
        store = InMemoryStateStore()
        email_provider = MockEmailProvider()
        approval_store, gateway = _make_gateway(store, email_provider)
        specialist = SendingSpecialist(
            email_provider=email_provider,
            settings=_settings_multi_user(),
            gateway=gateway,
        )
        run = _make_run()
        approval = _create_gate2_approval(approval_store, run, run.draft.body)
        run.pending_approval_id = approval.id

        result = await specialist(run)

        assert result["status"] == "sent"
        assert result["message_id"]
        assert email_provider.call_count == 1
        # The approval was consumed (single-use).
        consumed = approval_store.get(approval.id)
        from opsmesh.models import ApprovalStatus
        assert consumed.status == ApprovalStatus.CONSUMED

    @pytest.mark.asyncio
    async def test_send_with_modified_body_is_rejected(self):
        """Stage B Phase 2 (P0-1): if the body is modified after approval,
        the payload_hash mismatches → StaleApprovalError → send fails closed.
        The provider is NEVER called (the gateway blocks before dispatch)."""
        store = InMemoryStateStore()
        email_provider = MockEmailProvider()
        approval_store, gateway = _make_gateway(store, email_provider)
        specialist = SendingSpecialist(
            email_provider=email_provider,
            settings=_settings_multi_user(),
            gateway=gateway,
        )
        run = _make_run(draft_body="Original approved body.")
        # Approve the ORIGINAL body.
        approval = _create_gate2_approval(approval_store, run, "Original approved body.")
        run.pending_approval_id = approval.id
        # Tamper with the body AFTER approval.
        run.draft.body = "TAMPERED body — different content."

        result = await specialist(run)

        assert result["status"] == "failed"
        assert "stale_approval" in result["error"]
        # CRITICAL: the provider was never called (the gateway blocked it).
        assert email_provider.call_count == 0


# ---------------------------------------------------------------------------
# 2. Send with unverified (synthesized) approval fails closed
# ---------------------------------------------------------------------------


class TestSendWithUnverifiedApprovalFailsClosed:
    @pytest.mark.asyncio
    async def test_synthesized_approval_id_is_rejected(self):
        """Stage B Phase 2 (P0-1): the old bug synthesized approval_id =
        f"ap_{run_hex}" (derived from run_id, NOT a real approval record).
        With the gateway wired, that fake ID is not in the ApprovalStore →
        approval_not_found → send fails closed. The provider is never called."""
        store = InMemoryStateStore()
        email_provider = MockEmailProvider()
        approval_store, gateway = _make_gateway(store, email_provider)
        specialist = SendingSpecialist(
            email_provider=email_provider,
            settings=_settings_multi_user(),
            gateway=gateway,
        )
        run = _make_run()
        # Synthesize the OLD fake approval_id (the P0-1 bug).
        run_hex = run.run_id.replace("run_", "").replace("-", "")[:12].ljust(12, "0")
        run.pending_approval_id = f"ap_{run_hex}"

        result = await specialist(run)

        assert result["status"] == "failed"
        assert "stale_approval" in result["error"]
        assert "approval_not_found" in result["error"]
        assert email_provider.call_count == 0

    @pytest.mark.asyncio
    async def test_empty_approval_id_is_rejected(self):
        """No pending_approval_id at all → fail closed (no send)."""
        store = InMemoryStateStore()
        email_provider = MockEmailProvider()
        _approval_store, gateway = _make_gateway(store, email_provider)
        specialist = SendingSpecialist(
            email_provider=email_provider,
            settings=_settings_multi_user(),
            gateway=gateway,
        )
        run = _make_run()
        run.pending_approval_id = None  # no Gate 2 approval

        result = await specialist(run)

        assert result["status"] == "failed"
        assert "No Gate 2 approval" in result["error"]
        assert email_provider.call_count == 0


# ---------------------------------------------------------------------------
# 3. Gateway enforced on create_lead — requires approval + workspace scope
# ---------------------------------------------------------------------------


class TestGatewayEnforcedOnCrmCreate:
    @pytest.mark.asyncio
    async def test_create_lead_requires_approval(self):
        """create_lead is a Gate-1-gated tool: calling it without an
        approval_id raises ApprovalRequiredError (never reaches the provider)."""
        from opsmesh.mcp.base import MCPClient
        approval_store = InMemoryApprovalStore()
        verifier = RealApprovalVerifier(store=approval_store)
        gateway = ToolGateway(
            client=MCPClient(),
            store=InMemoryStateStore(),
            approval_verifier=verifier,
        )
        auth = AuthContext(workspace_id="ws_test", scopes=["crm.write"])
        with pytest.raises(GatewayError) as exc:
            await gateway.execute(
                "create_lead",
                {"company": "Acme", "contact_email": "founder@acme.com"},
                auth=auth,
                run_id="run_001",
                step_id=1,
                approval_id=None,
            )
        assert exc.value.code == "approval_required"

    @pytest.mark.asyncio
    async def test_create_lead_requires_workspace_scope(self):
        """A caller without crm.write scope is rejected (insufficient_scope)."""
        from opsmesh.mcp.base import MCPClient
        approval_store = InMemoryApprovalStore()
        verifier = RealApprovalVerifier(store=approval_store)
        gateway = ToolGateway(
            client=MCPClient(),
            store=InMemoryStateStore(),
            approval_verifier=verifier,
        )
        # Caller has NO scopes.
        auth = AuthContext(workspace_id="ws_test", scopes=[])
        with pytest.raises(GatewayError) as exc:
            await gateway.execute(
                "create_lead",
                {"company": "Acme", "contact_email": "founder@acme.com"},
                auth=auth,
                run_id="run_001",
                step_id=1,
                approval_id="ap_anything",
            )
        assert exc.value.code == "insufficient_scope"

    @pytest.mark.asyncio
    async def test_create_lead_rejects_wrong_workspace_approval(self):
        """An approval from workspace A cannot authorize a call in workspace B."""
        from opsmesh.mcp.base import MCPClient
        from opsmesh.models import ApprovalStatus
        approval_store = InMemoryApprovalStore()
        # Create an approval in workspace A.
        rec = approval_store.create(
            workspace_id="ws_A",
            run_id="run_001",
            kind=ApprovalKind.EVIDENCE_REVIEW,
            action="create_lead",
            payload={"company": "Acme", "contact_email": "f@acme.com"},
            payload_hash=compute_payload_hash(
                "create_lead",
                {"company": "Acme", "contact_email": "f@acme.com"},
                TOOL_PROTECTED_FIELDS["create_lead"],
            ),
        )
        rec.status = ApprovalStatus.APPROVED
        approval_store._approvals[rec.id] = rec

        verifier = RealApprovalVerifier(store=approval_store)
        gateway = ToolGateway(
            client=MCPClient(),
            store=InMemoryStateStore(),
            approval_verifier=verifier,
        )
        # Call from workspace B with workspace A's approval.
        auth = AuthContext(workspace_id="ws_B", scopes=["crm.write"])
        with pytest.raises(GatewayError) as exc:
            await gateway.execute(
                "create_lead",
                {"company": "Acme", "contact_email": "f@acme.com"},
                auth=auth,
                run_id="run_001",
                step_id=1,
                approval_id=rec.id,
            )
        assert exc.value.code == "stale_approval"
        assert "workspace" in exc.value.message.lower()


# ---------------------------------------------------------------------------
# 4. Bypass attempt rejected — the specialist routes through the gateway
# ---------------------------------------------------------------------------


class TestGatewayBypassAttemptRejected:
    @pytest.mark.asyncio
    async def test_specialist_with_gateway_does_not_call_provider_directly(self):
        """When the gateway is wired, a payload mismatch NEVER reaches the
        provider. The specialist routes through gateway.execute, which blocks
        before dispatch. This proves there is no bypass path."""
        store = InMemoryStateStore()
        email_provider = MockEmailProvider()
        approval_store, gateway = _make_gateway(store, email_provider)
        specialist = SendingSpecialist(
            email_provider=email_provider,
            settings=_settings_multi_user(),
            gateway=gateway,
        )
        run = _make_run(draft_body="Approved body.")
        approval = _create_gate2_approval(approval_store, run, "Approved body.")
        run.pending_approval_id = approval.id
        # Tamper — should be blocked by the gateway, never reach the provider.
        run.draft.body = "Tampered."

        result = await specialist(run)

        assert result["status"] == "failed"
        assert email_provider.call_count == 0
        # The gateway recorded the rejection in its audit log.
        rejections = [e for e in gateway.audit_entries if e["outcome"] == "rejected"]
        assert len(rejections) >= 1
        assert "stale_approval" in rejections[0]["rejection_reason"]
