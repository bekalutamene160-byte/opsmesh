"""
Tests for Phase 2: Real provider adapters (Groq, Exa, Resend, HubSpot).

All tests use httpx.MockTransport — NO real API calls are made.
Mocks remain the default for CI. These tests verify the adapter contracts.
"""

from __future__ import annotations

import httpx
import pytest

from opsmesh.adapters.llm.base import LLMProvider, LLMResponse, Message
from opsmesh.adapters.llm.groq import GroqProvider, GROQ_BASE_URL
from opsmesh.adapters.research.exa import ExaResearchProvider, EXA_BASE_URL
from opsmesh.adapters.email.resend import ResendEmailProvider, RESEND_BASE_URL
from opsmesh.adapters.email.base import EmailMessage, EmailStatus
from opsmesh.adapters.crm.hubspot import HubSpotCRMProvider, HUBSPOT_BASE_URL
from opsmesh.adapters.crm.base import CRMLeadCreate, ToolError
from opsmesh.adapters.provider_factory import (
    create_llm_provider, create_research_provider,
    create_email_provider, create_crm_provider,
)
from opsmesh.config import Settings, get_settings


def make_mock_client(base_url, handler, headers=None):
    """Create an httpx.AsyncClient with MockTransport + base_url."""
    return httpx.AsyncClient(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
        headers=headers or {},
    )


# ===========================================================================
# 1. Groq Provider
# ===========================================================================


class TestGroqProvider:
    @pytest.mark.asyncio
    async def test_chat_returns_response(self):
        def handler(request):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })

        provider = GroqProvider(api_key="test-key", model="test-model")
        provider._client = make_mock_client(GROQ_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        result = await provider.chat([Message(role="user", content="Hello")])

        assert result.content == "Hello!"
        assert result.input_tokens == 10
        assert result.cost_usd > 0
        assert result.finish_reason == "stop"
        await provider.close()

    @pytest.mark.asyncio
    async def test_chat_structured_output(self):
        from opsmesh.models import ResearchResult

        def handler(request):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": '{"evidence":[],"missing_info":[],"summary":"ok","sufficient_for_qualification":true}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            })

        provider = GroqProvider(api_key="test-key")
        provider._client = make_mock_client(GROQ_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        result = await provider.chat([Message(role="user", content="Research")], response_schema=ResearchResult)
        assert result.structured is not None
        assert isinstance(result.structured, ResearchResult)
        assert result.structured.sufficient_for_qualification is True
        await provider.close()

    @pytest.mark.asyncio
    async def test_rate_limit_raises(self):
        def handler(request):
            return httpx.Response(429, headers={"retry-after": "30"})

        provider = GroqProvider(api_key="test-key")
        provider._client = make_mock_client(GROQ_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        from opsmesh.retry import RateLimitError
        with pytest.raises(RateLimitError) as exc:
            await provider.chat([Message(role="user", content="Hi")])
        assert exc.value.retry_after == 30.0
        await provider.close()

    @pytest.mark.asyncio
    async def test_server_error_raises_transient(self):
        def handler(request):
            return httpx.Response(503, text="Unavailable")

        provider = GroqProvider(api_key="test-key")
        provider._client = make_mock_client(GROQ_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        from opsmesh.retry import TransientError
        with pytest.raises(TransientError):
            await provider.chat([Message(role="user", content="Hi")])
        await provider.close()

    @pytest.mark.asyncio
    async def test_client_error_raises(self):
        def handler(request):
            return httpx.Response(400, text="Bad Request")

        provider = GroqProvider(api_key="test-key")
        provider._client = make_mock_client(GROQ_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        with pytest.raises(ValueError):
            await provider.chat([Message(role="user", content="Hi")])
        await provider.close()


# ===========================================================================
# 2. Exa Research Provider
# ===========================================================================


class TestExaProvider:
    @pytest.mark.asyncio
    async def test_search_returns_hits(self):
        def handler(request):
            return httpx.Response(200, json={
                "results": [
                    {"url": "https://acme.com", "title": "Acme Corp", "text": "Acme is a software company."},
                    {"url": "https://acme.com/about", "title": "About Acme", "text": "Founded in 2020."},
                ]
            })

        provider = ExaResearchProvider(api_key="test-key")
        provider._client = make_mock_client(EXA_BASE_URL, handler, {"x-api-key": "test-key"})

        hits = await provider.web_search("acme corp", max_results=5)
        assert len(hits) == 2
        assert "acme.com" in str(hits[0].url)
        assert hits[0].title == "Acme Corp"
        assert "software" in hits[0].snippet
        await provider.close()

    @pytest.mark.asyncio
    async def test_empty_results(self):
        def handler(request):
            return httpx.Response(200, json={"results": []})

        provider = ExaResearchProvider(api_key="test-key")
        provider._client = make_mock_client(EXA_BASE_URL, handler, {"x-api-key": "test-key"})

        hits = await provider.web_search("nonexistent")
        assert hits == []
        await provider.close()

    @pytest.mark.asyncio
    async def test_rate_limit_raises(self):
        def handler(request):
            return httpx.Response(429)

        provider = ExaResearchProvider(api_key="test-key")
        provider._client = make_mock_client(EXA_BASE_URL, handler, {"x-api-key": "test-key"})

        from opsmesh.retry import RateLimitError
        with pytest.raises(RateLimitError):
            await provider.web_search("test")
        await provider.close()


# ===========================================================================
# 3. Resend Email Provider
# ===========================================================================


class TestResendProvider:
    @pytest.mark.asyncio
    async def test_send_returns_message_id(self):
        def handler(request):
            return httpx.Response(200, json={"id": "msg_abc123"})

        provider = ResendEmailProvider(api_key="test-key")
        provider._client = make_mock_client(RESEND_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        msg = EmailMessage(to="r@acme.com", from_="s@x.com", subject="Hi", body="Body", approval_id="ap_abcdef123456")
        result = await provider.send(msg)
        assert result.message_id == "msg_abc123"
        assert result.status == "sent"
        await provider.close()

    @pytest.mark.asyncio
    async def test_get_status_confirmed_sent(self):
        def handler(request):
            return httpx.Response(200, json={"status": "delivered"})

        provider = ResendEmailProvider(api_key="test-key")
        provider._client = make_mock_client(RESEND_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        status = await provider.get_status("msg_abc123")
        assert status.status == "confirmed_sent"
        await provider.close()

    @pytest.mark.asyncio
    async def test_get_status_confirmed_failed(self):
        def handler(request):
            return httpx.Response(200, json={"status": "bounced"})

        provider = ResendEmailProvider(api_key="test-key")
        provider._client = make_mock_client(RESEND_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        status = await provider.get_status("msg_abc123")
        assert status.status == "confirmed_failed"
        await provider.close()

    @pytest.mark.asyncio
    async def test_get_status_unknown(self):
        def handler(request):
            return httpx.Response(200, json={"status": "queued"})

        provider = ResendEmailProvider(api_key="test-key")
        provider._client = make_mock_client(RESEND_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        status = await provider.get_status("msg_abc123")
        assert status.status == "unknown"
        await provider.close()

    @pytest.mark.asyncio
    async def test_get_status_not_found(self):
        def handler(request):
            return httpx.Response(404)

        provider = ResendEmailProvider(api_key="test-key")
        provider._client = make_mock_client(RESEND_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        status = await provider.get_status("msg_nonexistent")
        assert status.status == "unknown"
        assert "not found" in status.detail.lower()
        await provider.close()

    @pytest.mark.asyncio
    async def test_rate_limit_raises(self):
        def handler(request):
            return httpx.Response(429, headers={"retry-after": "60"})

        provider = ResendEmailProvider(api_key="test-key")
        provider._client = make_mock_client(RESEND_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        from opsmesh.retry import RateLimitError
        msg = EmailMessage(to="r@acme.com", from_="s@x.com", subject="x", body="x", approval_id="ap_abcdef123456")
        with pytest.raises(RateLimitError):
            await provider.send(msg)
        await provider.close()


# ===========================================================================
# 4. HubSpot CRM Provider
# ===========================================================================


class TestHubSpotProvider:
    @pytest.mark.asyncio
    async def test_get_lead_returns_lead(self):
        def handler(request):
            return httpx.Response(200, json={
                "properties": {
                    "email": "founder@acme.com",
                    "firstname": "Jane", "lastname": "Doe",
                    "company": "Acme", "industry": "Software",
                }
            })

        provider = HubSpotCRMProvider(api_key="test-key")
        provider._client = make_mock_client(HUBSPOT_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        lead = await provider.get_lead("ws_1", "L_123")
        assert lead is not None
        assert lead.company == "Acme"
        assert lead.contact_email == "founder@acme.com"
        assert lead.contact_name == "Jane Doe"
        await provider.close()

    @pytest.mark.asyncio
    async def test_get_lead_not_found(self):
        def handler(request):
            return httpx.Response(404)

        provider = HubSpotCRMProvider(api_key="test-key")
        provider._client = make_mock_client(HUBSPOT_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        lead = await provider.get_lead("ws_1", "L_999")
        assert lead is None
        await provider.close()

    @pytest.mark.asyncio
    async def test_create_lead(self):
        def handler(request):
            return httpx.Response(201, json={"id": "12345"})

        provider = HubSpotCRMProvider(api_key="test-key")
        provider._client = make_mock_client(HUBSPOT_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        args = CRMLeadCreate(workspace_id="ws_1", company="Acme", contact_email="founder@acme.com", contact_name="Jane Doe")
        result = await provider.create_lead(args)
        assert result.lead_id == "L_12345"
        await provider.close()

    @pytest.mark.asyncio
    async def test_create_lead_duplicate(self):
        def handler(request):
            return httpx.Response(409)

        provider = HubSpotCRMProvider(api_key="test-key")
        provider._client = make_mock_client(HUBSPOT_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        args = CRMLeadCreate(workspace_id="ws_1", company="Acme", contact_email="founder@acme.com")
        with pytest.raises(ToolError) as exc:
            await provider.create_lead(args)
        assert exc.value.code == "duplicate"
        await provider.close()

    @pytest.mark.asyncio
    async def test_update_lead_status(self):
        def handler(request):
            return httpx.Response(200, json={})

        provider = HubSpotCRMProvider(api_key="test-key")
        provider._client = make_mock_client(HUBSPOT_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        result = await provider.update_lead_status("ws_1", "L_123", "contacted")
        assert result.new_status == "contacted"
        await provider.close()

    @pytest.mark.asyncio
    async def test_update_lead_not_found(self):
        def handler(request):
            return httpx.Response(404)

        provider = HubSpotCRMProvider(api_key="test-key")
        provider._client = make_mock_client(HUBSPOT_BASE_URL, handler, {"Authorization": "Bearer test-key"})

        with pytest.raises(ToolError) as exc:
            await provider.update_lead_status("ws_1", "L_999", "contacted")
        assert exc.value.code == "not_found"
        await provider.close()


# ===========================================================================
# 5. Provider Factory
# ===========================================================================


class TestProviderFactory:
    def test_creates_mock_llm_by_default(self):
        get_settings.cache_clear()
        import os
        os.environ.pop("OPSMESH_LLM_PROVIDER", None)
        os.environ.pop("OPSMESH_LLM_API_KEY", None)
        get_settings.cache_clear()
        provider = create_llm_provider(Settings())
        from opsmesh.adapters.llm.mock import MockLLMProvider
        assert isinstance(provider, MockLLMProvider)

    def test_creates_groq_when_configured(self):
        get_settings.cache_clear()
        import os
        os.environ["OPSMESH_LLM_PROVIDER"] = "groq"
        os.environ["OPSMESH_LLM_API_KEY"] = "test-key"
        get_settings.cache_clear()
        provider = create_llm_provider(Settings())
        assert isinstance(provider, GroqProvider)
        os.environ.pop("OPSMESH_LLM_PROVIDER", None)
        os.environ.pop("OPSMESH_LLM_API_KEY", None)
        get_settings.cache_clear()

    def test_creates_mock_research_by_default(self):
        get_settings.cache_clear()
        provider = create_research_provider(Settings())
        from opsmesh.adapters.research.mock import MockResearchProvider
        assert isinstance(provider, MockResearchProvider)

    def test_creates_exa_when_configured(self):
        get_settings.cache_clear()
        import os
        os.environ["OPSMESH_EXA_API_KEY"] = "test-key"
        get_settings.cache_clear()
        provider = create_research_provider(Settings())
        assert isinstance(provider, ExaResearchProvider)
        os.environ.pop("OPSMESH_EXA_API_KEY", None)
        get_settings.cache_clear()

    def test_creates_mock_email_by_default(self):
        get_settings.cache_clear()
        provider = create_email_provider(Settings())
        from opsmesh.adapters.email.mock import MockEmailProvider
        assert isinstance(provider, MockEmailProvider)

    def test_creates_mock_crm_by_default(self):
        get_settings.cache_clear()
        provider = create_crm_provider(Settings())
        from opsmesh.adapters.crm.mock import MockCRMProvider
        assert isinstance(provider, MockCRMProvider)

    def test_groq_without_api_key_raises(self):
        get_settings.cache_clear()
        import os
        os.environ["OPSMESH_LLM_PROVIDER"] = "groq"
        os.environ.pop("OPSMESH_LLM_API_KEY", None)
        get_settings.cache_clear()
        with pytest.raises(ValueError, match="requires OPSMESH_LLM_API_KEY"):
            create_llm_provider(Settings())
        os.environ.pop("OPSMESH_LLM_PROVIDER", None)
        get_settings.cache_clear()
