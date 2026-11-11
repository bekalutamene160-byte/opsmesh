"""
Tests for Phase 7.1: Real provider adapters (Groq, Exa, Resend, HubSpot).

These tests use httpx mocking (via pytest-httpx or monkeypatching) — they
do NOT make real API calls. This keeps CI deterministic and free.

To run real smoke tests with your actual API keys, see
tests/integration/test_real_providers.py (run manually with:
  OPSMESH_LLM_API_KEY=gsk_... pytest tests/integration/ -v
)
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from opsmesh.adapters.llm.base import LLMProvider, LLMResponse, Message
from opsmesh.adapters.llm.mock import MockLLMProvider
from opsmesh.adapters.research.base import ResearchProvider
from opsmesh.adapters.research.mock import MockResearchProvider
from opsmesh.adapters.email.base import EmailMessage, EmailProvider, EmailSendResult, EmailStatus
from opsmesh.adapters.email.mock import MockEmailProvider
from opsmesh.adapters.crm.base import CRMLead, CRMLeadCreate, CRMProvider
from opsmesh.adapters.crm.mock import MockCRMProvider


# ===========================================================================
# 1. Provider factory — selects the right adapter based on settings
# ===========================================================================


class TestProviderFactory:
    def test_mock_llm_by_default(self):
        from opsmesh.adapters.provider_factory import create_llm_provider
        from opsmesh.config import Settings
        settings = Settings(llm_provider="mock")
        provider = create_llm_provider(settings)
        assert isinstance(provider, MockLLMProvider)

    def test_groq_provider_when_configured(self):
        from opsmesh.adapters.provider_factory import create_llm_provider
        from opsmesh.config import Settings
        settings = Settings(
            llm_provider="groq",
            llm_api_key="gsk_test_key",
            llm_default_model="openai/gpt-oss-120b",
        )
        provider = create_llm_provider(settings)
        # GroqProvider is created lazily — just check it's not a Mock
        assert not isinstance(provider, MockLLMProvider)
        assert hasattr(provider, "default_model")
        assert provider.default_model == "openai/gpt-oss-120b"

    def test_groq_provider_requires_api_key(self):
        from opsmesh.adapters.provider_factory import create_llm_provider
        from opsmesh.config import Settings
        settings = Settings(llm_provider="groq", llm_api_key="")
        with pytest.raises(ValueError, match="OPSMESH_LLM_API_KEY"):
            create_llm_provider(settings)

    def test_exa_research_provider_when_configured(self):
        from opsmesh.adapters.provider_factory import create_research_provider
        from opsmesh.config import Settings
        settings = Settings(research_provider="exa", exa_api_key="exa_test_key")
        provider = create_research_provider(settings)
        assert not isinstance(provider, MockResearchProvider)

    def test_resend_email_provider_when_configured(self):
        from opsmesh.adapters.provider_factory import create_email_provider
        from opsmesh.config import Settings
        settings = Settings(email_provider="resend", email_api_key="re_test_key")
        provider = create_email_provider(settings)
        assert not isinstance(provider, MockEmailProvider)

    def test_hubspot_crm_provider_when_configured(self):
        from opsmesh.adapters.provider_factory import create_crm_provider
        from opsmesh.config import Settings
        settings = Settings(crm_provider="hubspot", crm_api_key="hub_test_token")
        provider = create_crm_provider(settings)
        assert not isinstance(provider, MockCRMProvider)


# ===========================================================================
# 2. Groq adapter — model + pricing
# ===========================================================================


class TestGroqAdapter:
    def test_default_model_is_gpt_oss_120b(self):
        """Phase 7: the default Groq model is now GPT-OSS 120B (Llama 3.3 is Enterprise-only)."""
        from opsmesh.adapters.llm.groq import GroqProvider
        provider = GroqProvider(api_key="gsk_test", model="openai/gpt-oss-120b")
        assert provider.default_model == "openai/gpt-oss-120b"

    def test_pricing_table_has_gpt_oss_models(self):
        from opsmesh.adapters.llm.groq import MODEL_PRICING
        # GPT-OSS 120B (flagship replacement for Llama 3.3 70B)
        assert "openai/gpt-oss-120b" in MODEL_PRICING
        pricing = MODEL_PRICING["openai/gpt-oss-120b"]
        assert pricing["input"] == 0.15
        assert pricing["output"] == 0.60
        # GPT-OSS 20B (cheaper alternative)
        assert "openai/gpt-oss-20b" in MODEL_PRICING

    def test_chat_calls_groq_api(self, monkeypatch):
        """Verify the adapter constructs the right request payload."""
        from opsmesh.adapters.llm.groq import GroqProvider

        captured_requests = []

        async def mock_post(self, url, json=None, timeout=None):
            captured_requests.append({
                "url": url,
                "json": json,
                "timeout": timeout,
            })
            # Return a fake Groq response
            mock_response = httpx.Response(
                200,
                json={
                    "choices": [{
                        "message": {"content": "Hello!"},
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                    },
                },
            )
            return mock_response

        # Patch the AsyncClient.post method
        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        provider = GroqProvider(api_key="gsk_test", model="openai/gpt-oss-120b")

        # Run the async chat() call in an event loop
        import asyncio
        result = asyncio.run(provider.chat(
            messages=[Message(role="user", content="Hi")],
            max_tokens=100,
        ))

        # Verify the request was made
        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert req["url"] == "/chat/completions"
        assert req["json"]["model"] == "openai/gpt-oss-120b"
        assert req["json"]["max_tokens"] == 100
        assert req["json"]["messages"][0]["role"] == "user"
        assert req["json"]["messages"][0]["content"] == "Hi"

        # Verify the response was parsed
        assert result.content == "Hello!"
        assert result.input_tokens == 10
        assert result.output_tokens == 5
        # Cost = (10 * 0.15 + 5 * 0.60) / 1M = (1.5 + 3.0) / 1M = 4.5e-6
        assert abs(result.cost_usd - 4.5e-6) < 1e-9


# ===========================================================================
# 3. Exa adapter — search
# ===========================================================================


class TestExaAdapter:
    def test_search_calls_exa_api(self, monkeypatch):
        from opsmesh.adapters.research.exa import ExaResearchProvider

        captured = []

        async def mock_post(self, url, json=None):
            captured.append({"url": url, "json": json})
            return httpx.Response(200, json={
                "results": [
                    {
                        "url": "https://acme.com",
                        "title": "Acme Corp",
                        "text": "Acme Corp is a leading software company.",
                    },
                    {
                        "url": "https://news.com/acme",
                        "title": "Acme raises $50M",
                        "text": "Acme Corp announced a $50M Series B.",
                    },
                ],
            })

        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        provider = ExaResearchProvider(api_key="exa_test")
        import asyncio
        hits = asyncio.run(provider.web_search("acme corp software", max_results=5))

        assert len(captured) == 1
        assert captured[0]["url"] == "/search"
        assert captured[0]["json"]["query"] == "acme corp software"
        assert captured[0]["json"]["numResults"] == 5

        assert len(hits) == 2
        # Pydantic converts URLs to HttpUrl objects — compare as strings
        assert str(hits[0].url).rstrip("/") == "https://acme.com"
        assert hits[0].title == "Acme Corp"
        assert "software" in hits[0].snippet


# ===========================================================================
# 4. Resend adapter — send + get_status
# ===========================================================================


class TestResendAdapter:
    def test_send_calls_resend_api(self, monkeypatch):
        from opsmesh.adapters.email.resend import ResendEmailProvider

        captured = []

        async def mock_post(self, url, json=None, headers=None):
            captured.append({"url": url, "json": json, "headers": headers})
            return httpx.Response(200, json={"id": "re_msg_abc123"})

        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        provider = ResendEmailProvider(api_key="re_test")
        msg = EmailMessage(
            from_="Bekalu <bekalu@opsmesh.dev>",
            to="founder@startup.com",
            subject="Hello from OPSMESH",
            body="This is a test email.",
            approval_id="ap_abcdef123456",  # required pattern: ap_ + 12 hex chars
            idempotency_key="idem_123",
        )
        import asyncio
        result = asyncio.run(provider.send(msg))

        assert len(captured) == 1
        assert captured[0]["url"] == "/emails"
        assert captured[0]["json"]["from"] == "Bekalu <bekalu@opsmesh.dev>"
        assert captured[0]["json"]["to"] == "founder@startup.com"
        assert captured[0]["headers"]["Idempotency-Key"] == "idem_123"

        assert result.message_id == "re_msg_abc123"
        assert result.status == "sent"

    def test_get_status_returns_unknown_on_404(self, monkeypatch):
        from opsmesh.adapters.email.resend import ResendEmailProvider

        async def mock_get(self, url):
            return httpx.Response(404)

        monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

        provider = ResendEmailProvider(api_key="re_test")
        import asyncio
        status = asyncio.run(provider.get_status("re_msg_unknown"))

        assert status.status == "unknown"
        assert "not found" in status.detail.lower()

    def test_status_map_resolves_correctly(self):
        from opsmesh.adapters.email.resend import STATUS_MAP
        assert STATUS_MAP["delivered"] == "confirmed_sent"
        assert STATUS_MAP["bounced"] == "confirmed_failed"
        assert STATUS_MAP["queued"] == "unknown"


# ===========================================================================
# 5. HubSpot adapter — create_lead, update_lead_status
# ===========================================================================


class TestHubSpotAdapter:
    def test_create_lead_calls_hubspot_api(self, monkeypatch):
        from opsmesh.adapters.crm.hubspot import HubSpotCRMProvider

        captured = []

        async def mock_post(self, url, json=None):
            captured.append({"url": url, "json": json})
            return httpx.Response(201, json={"id": "123456"})

        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        provider = HubSpotCRMProvider(api_key="hub_test_token")
        import asyncio
        result = asyncio.run(provider.create_lead(CRMLeadCreate(
            workspace_id="ws_1",
            company="Acme Corp",
            contact_email="founder@acme.com",
            contact_name="John Smith",
            industry="Software",
        )))

        assert len(captured) == 1
        assert captured[0]["url"] == "/crm/v3/objects/contacts"
        props = captured[0]["json"]["properties"]
        assert props["email"] == "founder@acme.com"
        assert props["firstname"] == "John"
        assert props["lastname"] == "Smith"
        assert props["company"] == "Acme Corp"

        # Result format: L_<hubspot_id>
        assert result.lead_id == "L_123456"

    def test_status_mapping(self):
        from opsmesh.adapters.crm.hubspot import STATUS_TO_HUBSPOT
        assert STATUS_TO_HUBSPOT["new"] == "lead"
        assert STATUS_TO_HUBSPOT["contacted"] == "customer"
        assert STATUS_TO_HUBSPOT["qualified"] == "qualified_lead"


# ===========================================================================
# 6. Integrations endpoint reports real provider config
# ===========================================================================


class TestIntegrationsWithRealProviders:
    def test_integrations_reports_groq_when_configured(self):
        from fastapi.testclient import TestClient
        from opsmesh.api.app import create_app
        from opsmesh.config import get_settings

        os.environ["OPSMESH_LLM_PROVIDER"] = "groq"
        os.environ["OPSMESH_LLM_API_KEY"] = "gsk_test_key"
        os.environ["OPSMESH_LLM_DEFAULT_MODEL"] = "openai/gpt-oss-120b"
        get_settings.cache_clear()

        try:
            app = create_app(webhook_secret="test-secret")
            client = TestClient(app)
            r = client.get("/api/v1/integrations")
            assert r.status_code == 200
            data = r.json()
            assert data["llm"]["provider"] == "groq"
            assert data["llm"]["model"] == "openai/gpt-oss-120b"
            assert data["llm"]["api_key_configured"] is True
            assert data["llm"]["status"] == "configured"
        finally:
            os.environ.pop("OPSMESH_LLM_PROVIDER", None)
            os.environ.pop("OPSMESH_LLM_API_KEY", None)
            os.environ.pop("OPSMESH_LLM_DEFAULT_MODEL", None)
            get_settings.cache_clear()
