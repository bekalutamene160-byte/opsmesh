"""
Real provider smoke tests — these make ACTUAL API calls.

DO NOT run these in CI. Run them manually to verify your API keys work:

    # Set your real keys
    export OPSMESH_LLM_API_KEY="gsk_..."
    export OPSMESH_EXA_API_KEY="..."
    export OPSMESH_EMAIL_API_KEY="re_..."
    export OPSMESH_CRM_API_KEY="..."
    export OPSMESH_EMAIL_FROM="you@yourdomain.com"

    # Run only the integration tests
    pytest tests/integration/test_real_providers.py -v -m "not slow"

Each test is independent and can be run individually. If a test fails,
the error message will tell you which provider/key to check.
"""

from __future__ import annotations

import os

import pytest


# Skip all tests in this module unless the corresponding API key is set
pytestmark = pytest.mark.skipif(
    not os.environ.get("OPSMESH_LLM_API_KEY"),
    reason="Set OPSMESH_LLM_API_KEY to run real provider tests",
)


@pytest.mark.asyncio
async def test_groq_real_chat_completion():
    """Make a real Groq API call with GPT-OSS 120B.

    Verifies:
      - The API key is valid
      - The model `openai/gpt-oss-120b` is available on your tier
      - The response is parsed correctly
    """
    from opsmesh.adapters.llm.groq import GroqProvider
    from opsmesh.adapters.llm.base import Message

    provider = GroqProvider(
        api_key=os.environ["OPSMESH_LLM_API_KEY"],
        model="openai/gpt-oss-120b",
    )

    try:
        result = await provider.chat(
            messages=[
                Message(role="system", content="You are a helpful assistant. Reply in one short sentence."),
                Message(role="user", content="Say hello to OPSMESH."),
            ],
            max_tokens=50,
            temperature=0.0,
        )

        assert result.content, "Empty response from Groq"
        assert result.input_tokens > 0, "No input tokens reported"
        assert result.output_tokens > 0, "No output tokens reported"
        assert result.cost_usd > 0, "Cost should be > 0"
        assert result.model == "openai/gpt-oss-120b"
        print(f"\n  Groq response: {result.content!r}")
        print(f"  Tokens: {result.input_tokens} in + {result.output_tokens} out")
        print(f"  Cost: ${result.cost_usd:.6f}")
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_groq_structured_output():
    """Verify Groq returns valid JSON when response_schema is provided."""
    from opsmesh.adapters.llm.groq import GroqProvider
    from opsmesh.adapters.llm.base import Message
    from pydantic import BaseModel

    class LeadQualification(BaseModel):
        is_qualified: bool
        score: float
        reason: str

    provider = GroqProvider(
        api_key=os.environ["OPSMESH_LLM_API_KEY"],
        model="openai/gpt-oss-120b",
    )

    try:
        result = await provider.chat(
            messages=[
                Message(role="system", content="You are a lead qualification expert. Respond with JSON only."),
                Message(role="user", content=(
                    "Company: Acme Corp (Software, 50 employees, $5M ARR). "
                    "Qualify this lead. Score 0-1."
                )),
            ],
            response_schema=LeadQualification,
            max_tokens=200,
            temperature=0.0,
        )

        assert result.structured is not None, "Structured output should be parsed"
        assert isinstance(result.structured, LeadQualification)
        assert isinstance(result.structured.is_qualified, bool)
        assert 0 <= result.structured.score <= 1
        print(f"\n  Qualification: {result.structured.model_dump()}")
    finally:
        await provider.close()


# Exa research test (skipped if no EXA_API_KEY)
@pytest.mark.skipif(
    not os.environ.get("OPSMESH_EXA_API_KEY"),
    reason="Set OPSMESH_EXA_API_KEY to run this test",
)
@pytest.mark.asyncio
async def test_exa_real_search():
    """Make a real Exa search call."""
    from opsmesh.adapters.research.exa import ExaResearchProvider

    provider = ExaResearchProvider(api_key=os.environ["OPSMESH_EXA_API_KEY"])
    try:
        hits = await provider.web_search("Acme Corp software company", max_results=3)
        assert len(hits) > 0, "Exa returned no results"
        print(f"\n  Exa returned {len(hits)} hits:")
        for h in hits:
            print(f"    - {h.title}: {h.url}")
    finally:
        await provider.close()


# Resend email test (skipped if no EMAIL_API_KEY) — sends a real email
@pytest.mark.skipif(
    not os.environ.get("OPSMESH_EMAIL_API_KEY") or not os.environ.get("OPSMESH_TEST_EMAIL_TO"),
    reason="Set OPSMESH_EMAIL_API_KEY and OPSMESH_TEST_EMAIL_TO to run this test",
)
@pytest.mark.asyncio
async def test_resend_real_send():
    """Send a real email via Resend.

    ⚠️ This sends a real email to OPSMESH_TEST_EMAIL_TO.
    Make sure OPSMESH_EMAIL_FROM is set to a verified sender domain.
    """
    from opsmesh.adapters.email.resend import ResendEmailProvider
    from opsmesh.adapters.email.base import EmailMessage

    provider = ResendEmailProvider(api_key=os.environ["OPSMESH_EMAIL_API_KEY"])
    msg = EmailMessage(
        from_=os.environ.get("OPSMESH_EMAIL_FROM", "test@opsmesh.dev"),
        to=os.environ["OPSMESH_TEST_EMAIL_TO"],
        subject="OPSMESH Phase 7 smoke test",
        body="This is a test email from OPSMESH Phase 7. If you received this, Resend is working.",
        approval_id="ap_smoketest0001",  # matches the pattern ap_ + 12 hex chars
    )

    try:
        result = await provider.send(msg)
        assert result.message_id, "Resend returned no message_id"
        print(f"\n  Email sent! message_id: {result.message_id}")
        print(f"  Status: {result.status}")

        # Wait a moment, then check status
        import asyncio
        await asyncio.sleep(2)
        status = await provider.get_status(result.message_id)
        print(f"  Status after 2s: {status.status} ({status.detail})")
    finally:
        await provider.close()


# HubSpot CRM test (skipped if no CRM_API_KEY)
@pytest.mark.skipif(
    not os.environ.get("OPSMESH_CRM_API_KEY"),
    reason="Set OPSMESH_CRM_API_KEY to run this test",
)
@pytest.mark.asyncio
async def test_hubspot_real_create_lead():
    """Create a real contact in HubSpot CRM."""
    from opsmesh.adapters.crm.hubspot import HubSpotCRMProvider
    from opsmesh.adapters.crm.base import CRMLeadCreate

    provider = HubSpotCRMProvider(api_key=os.environ["OPSMESH_CRM_API_KEY"])
    try:
        result = await provider.create_lead(CRMLeadCreate(
            workspace_id="ws_smoke_test",
            company="OPSMESH Smoke Test Corp",
            contact_email="smoke-test-opsmesh@example.com",
            contact_name="Smoke Test",
            industry="Software",
        ))
        assert result.lead_id.startswith("L_"), f"Unexpected lead_id format: {result.lead_id}"
        print(f"\n  HubSpot contact created: {result.lead_id}")
        print(f"  (You can delete this test contact in HubSpot)")
    finally:
        await provider.close()
