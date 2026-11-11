"""
Tests for all 5 adapter families (Protocols + mocks).

For each adapter:
  - Protocol is runtime-checkable (isinstance works)
  - Mock implements the Protocol
  - Mock returns correct results
  - Workspace isolation is enforced (CRM)
  - Allowlists are enforced (CRM status)
  - Failure injection works (Email timeout, Email failure)
  - Call counting works (spy for tests)
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import HttpUrl

from opsmesh.adapters.crm.base import CRMLead, CRMLeadCreate, CRMProvider, ToolError
from opsmesh.adapters.crm.mock import MockCRMProvider, ALLOWED_STATUSES
from opsmesh.adapters.email.base import EmailMessage, EmailProvider
from opsmesh.adapters.email.mock import MockEmailProvider, ProviderError
from opsmesh.adapters.llm.base import LLMProvider, LLMResponse, Message
from opsmesh.adapters.llm.mock import MockLLMProvider, NoFixtureFound
from opsmesh.adapters.notifier.base import Notifier
from opsmesh.adapters.notifier.mock import MockNotifier
from opsmesh.adapters.research.base import ResearchProvider
from opsmesh.adapters.research.mock import MockResearchProvider
from opsmesh.models import LeadSource, utcnow
from opsmesh.tools.schemas import WebSearchHit


# ---------------------------------------------------------------------------
# LLM Adapter
# ---------------------------------------------------------------------------


class TestLLMAdapter:
    def test_protocol_is_runtime_checkable(self):
        provider = MockLLMProvider()
        assert isinstance(provider, LLMProvider)

    @pytest.mark.asyncio
    async def test_default_response(self):
        provider = MockLLMProvider(default_response="Hello world")
        response = await provider.chat([Message(role="user", content="hi")])
        assert response.content == "Hello world"
        assert response.model == "mock-llm"
        assert response.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_fixture_lookup(self):
        provider = MockLLMProvider()
        messages = [Message(role="user", content="test prompt")]
        canned = LLMResponse(
            content="canned response", input_tokens=5, output_tokens=3,
            cost_usd=0.001, model="mock-fixture",
        )
        provider.add_fixture(messages, canned)

        response = await provider.chat(messages)
        assert response.content == "canned response"
        assert response.model == "mock-fixture"

    @pytest.mark.asyncio
    async def test_no_fixture_raises(self):
        """Without a default and without a fixture, NoFixtureFound is raised.
        Tests fail loudly rather than silently passing with a wrong canned
        response."""
        provider = MockLLMProvider()
        with pytest.raises(NoFixtureFound):
            await provider.chat([Message(role="user", content="no fixture for this")])

    @pytest.mark.asyncio
    async def test_call_count_is_tracked(self):
        provider = MockLLMProvider(default_response="x")
        assert provider.call_count == 0
        await provider.chat([Message(role="user", content="hi")])
        await provider.chat([Message(role="user", content="hi")])
        assert provider.call_count == 2

    @pytest.mark.asyncio
    async def test_same_prompt_same_response(self):
        """Deterministic: same prompt → same fixture lookup → same response."""
        provider = MockLLMProvider()
        messages = [Message(role="user", content="deterministic test")]
        provider.add_fixture(
            messages,
            LLMResponse(content="A", input_tokens=1, output_tokens=1,
                        cost_usd=0.0, model="mock"),
        )
        r1 = await provider.chat(messages)
        r2 = await provider.chat(messages)
        assert r1.content == r2.content == "A"

    @pytest.mark.asyncio
    async def test_structured_output_parsing(self):
        from opsmesh.models import ResearchResult
        provider = MockLLMProvider()
        messages = [Message(role="user", content="research")]
        provider.add_fixture(
            messages,
            LLMResponse(
                content='{"evidence": [], "missing_info": [], "summary": "ok", "sufficient_for_qualification": true}',
                input_tokens=1, output_tokens=1, cost_usd=0.0, model="mock",
            ),
        )
        response = await provider.chat(messages, response_schema=ResearchResult)
        assert response.structured is not None
        assert isinstance(response.structured, ResearchResult)
        assert response.structured.sufficient_for_qualification is True


# ---------------------------------------------------------------------------
# CRM Adapter
# ---------------------------------------------------------------------------


class TestCRMAdapter:
    def test_protocol_is_runtime_checkable(self):
        crm = MockCRMProvider()
        assert isinstance(crm, CRMProvider)

    @pytest.mark.asyncio
    async def test_get_lead_returns_none_if_not_found(self):
        crm = MockCRMProvider()
        result = await crm.get_lead("ws_1", "L_999")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_lead_returns_lead_if_found(self):
        crm = MockCRMProvider()
        lead = CRMLead(
            lead_id="L_001", workspace_id="ws_1",
            company="Acme", contact_email="r@acme.com",
        )
        crm.add_lead(lead)
        result = await crm.get_lead("ws_1", "L_001")
        assert result is not None
        assert result.company == "Acme"

    @pytest.mark.asyncio
    async def test_workspace_isolation(self):
        """Cross-workspace get_lead returns None (treated as 404)."""
        crm = MockCRMProvider()
        lead = CRMLead(
            lead_id="L_001", workspace_id="ws_1",
            company="Acme", contact_email="r@acme.com",
        )
        crm.add_lead(lead)
        # Try to access from a different workspace
        result = await crm.get_lead("ws_2", "L_001")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_status_valid(self):
        crm = MockCRMProvider()
        lead = CRMLead(
            lead_id="L_001", workspace_id="ws_1",
            company="Acme", contact_email="r@acme.com",
        )
        crm.add_lead(lead)
        result = await crm.update_lead_status("ws_1", "L_001", "contacted")
        assert result.new_status == "contacted"
        assert result.previous_status == "new"

    @pytest.mark.asyncio
    async def test_update_status_invalid_rejected(self):
        """Defense in depth: even 'admin' is rejected before reaching the
        ToolGateway (Day 4 adds gateway check on top)."""
        crm = MockCRMProvider()
        lead = CRMLead(
            lead_id="L_001", workspace_id="ws_1",
            company="Acme", contact_email="r@acme.com",
        )
        crm.add_lead(lead)
        with pytest.raises(ToolError) as exc_info:
            await crm.update_lead_status("ws_1", "L_001", "admin")
        assert exc_info.value.code == "invalid_status"

    @pytest.mark.asyncio
    async def test_update_status_not_found(self):
        crm = MockCRMProvider()
        with pytest.raises(ToolError) as exc_info:
            await crm.update_lead_status("ws_1", "L_999", "contacted")
        assert exc_info.value.code == "not_found"

    @pytest.mark.asyncio
    async def test_create_lead(self):
        crm = MockCRMProvider()
        args = CRMLeadCreate(
            workspace_id="ws_1",
            company="Globex",
            contact_email="founder@globex.com",
            industry="Manufacturing",
        )
        result = await crm.create_lead(args)
        assert result.lead_id.startswith("L_")
        # Verify the lead was created
        lead = await crm.get_lead("ws_1", result.lead_id)
        assert lead is not None
        assert lead.company == "Globex"

    @pytest.mark.asyncio
    async def test_allowlist_contents(self):
        """The status allowlist contains exactly the documented set."""
        assert ALLOWED_STATUSES == {
            "new", "contacted", "qualified", "rejected",
            "disqualified", "needs_review",
        }


# ---------------------------------------------------------------------------
# Email Adapter
# ---------------------------------------------------------------------------


class TestEmailAdapter:
    def test_protocol_is_runtime_checkable(self):
        email = MockEmailProvider()
        assert isinstance(email, EmailProvider)

    @pytest.mark.asyncio
    async def test_send_captures_message(self):
        email = MockEmailProvider()
        msg = EmailMessage(
            to="founder@acme.com",
            from_="bekalu@opsmesh.dev",
            subject="Hello",
            body="Body text",
            approval_id="ap_abcdef123456",
        )
        result = await email.send(msg)
        assert result.status == "sent"
        assert len(email.sent_messages) == 1
        assert email.sent_messages[0].to == "founder@acme.com"

    @pytest.mark.asyncio
    async def test_injected_timeout_raises(self):
        """Ambiguous timeout → asyncio.TimeoutError. The supervisor
        catches this and transitions to SEND_UNKNOWN (Correction 3)."""
        email = MockEmailProvider()
        email.inject_timeout()
        msg = EmailMessage(
            to="r@acme.com", from_="s@x.com",
            subject="x", body="x", approval_id="ap_abcdef123456",
        )
        with pytest.raises(asyncio.TimeoutError):
            await email.send(msg)
        # The message was NOT captured (send didn't complete)
        assert len(email.sent_messages) == 0

    @pytest.mark.asyncio
    async def test_injected_failure_raises(self):
        """Confirmed failure → ProviderError. The supervisor transitions
        the run to FAILED."""
        email = MockEmailProvider()
        email.inject_failure()
        msg = EmailMessage(
            to="r@acme.com", from_="s@x.com",
            subject="x", body="x", approval_id="ap_abcdef123456",
        )
        with pytest.raises(ProviderError):
            await email.send(msg)

    @pytest.mark.asyncio
    async def test_get_status_default_sent(self):
        email = MockEmailProvider()
        status = await email.get_status("msg_001")
        assert status.status == "confirmed_sent"

    @pytest.mark.asyncio
    async def test_get_status_override(self):
        """Tests can override status to simulate reconciler scenarios."""
        from opsmesh.adapters.email.base import EmailStatus
        email = MockEmailProvider()
        email.set_status_override(
            "msg_ambiguous",
            EmailStatus(message_id="msg_ambiguous", status="confirmed_failed"),
        )
        status = await email.get_status("msg_ambiguous")
        assert status.status == "confirmed_failed"

    @pytest.mark.asyncio
    async def test_call_count_tracked(self):
        email = MockEmailProvider()
        assert email.call_count == 0
        msg = EmailMessage(
            to="r@acme.com", from_="s@x.com",
            subject="x", body="x", approval_id="ap_abcdef123456",
        )
        await email.send(msg)
        await email.send(msg)
        assert email.call_count == 2


# ---------------------------------------------------------------------------
# Research Adapter
# ---------------------------------------------------------------------------


class TestResearchAdapter:
    def test_protocol_is_runtime_checkable(self):
        research = MockResearchProvider()
        assert isinstance(research, ResearchProvider)

    @pytest.mark.asyncio
    async def test_returns_registered_results(self):
        research = MockResearchProvider()
        hits = [
            WebSearchHit(url="https://acme.com", title="Acme", snippet="..."),
        ]
        research.add_result("acme", hits)
        results = await research.web_search("acme corp funding")
        assert len(results) == 1
        assert results[0].title == "Acme"

    @pytest.mark.asyncio
    async def test_returns_empty_for_unregistered_query(self):
        research = MockResearchProvider()
        results = await research.web_search("unknown company")
        assert results == []

    @pytest.mark.asyncio
    async def test_max_results_limit(self):
        research = MockResearchProvider()
        hits = [
            WebSearchHit(url=f"https://r{i}.com", title=f"R{i}", snippet="...")
            for i in range(10)
        ]
        research.add_result("acme", hits)
        results = await research.web_search("acme", max_results=3)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Notifier Adapter
# ---------------------------------------------------------------------------


class TestNotifierAdapter:
    def test_protocol_is_runtime_checkable(self):
        notifier = MockNotifier()
        assert isinstance(notifier, Notifier)

    @pytest.mark.asyncio
    async def test_send_captures_notification(self):
        notifier = MockNotifier()
        result = await notifier.send(
            to="workspace_owner",
            subject="Approval requested",
            body="Please review run X at https://dashboard/runs/X",
            kind="approval_requested",
        )
        assert result.channel == "email"
        assert len(notifier.sent) == 1
        assert notifier.sent[0]["kind"] == "approval_requested"

    @pytest.mark.asyncio
    async def test_call_count_tracked(self):
        notifier = MockNotifier()
        assert notifier.call_count == 0
        await notifier.send("x", "y", "z", "run_failed")
        assert notifier.call_count == 1
