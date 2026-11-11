"""
OPSMESH Provider Factory — reads Settings and returns the right adapter.

Architecture:
  This factory centralizes provider creation. Both the API and worker
  use this factory to get their adapters — ensuring they use the SAME
  configuration.

  When a provider env var is set to "mock" (default), the factory returns
  the mock adapter. When set to "groq", "exa", "resend", or "hubspot",
  the factory returns the real adapter.

USAGE:
    from opsmesh.adapters.provider_factory import (
        create_llm_provider, create_research_provider,
        create_email_provider, create_crm_provider,
    )
    from opsmesh.config import get_settings

    settings = get_settings()
    llm = create_llm_provider(settings)
    research = create_research_provider(settings)
    email = create_email_provider(settings)
    crm = create_crm_provider(settings)
"""

from __future__ import annotations

from opsmesh.adapters.crm.base import CRMProvider
from opsmesh.adapters.crm.mock import MockCRMProvider
from opsmesh.adapters.email.base import EmailProvider
from opsmesh.adapters.email.mock import MockEmailProvider
from opsmesh.adapters.llm.base import LLMProvider
from opsmesh.adapters.llm.mock import MockLLMProvider
from opsmesh.adapters.notifier.base import Notifier
from opsmesh.adapters.notifier.mock import MockNotifier
from opsmesh.adapters.research.base import ResearchProvider
from opsmesh.adapters.research.mock import MockResearchProvider
from opsmesh.config import Settings


def create_llm_provider(settings: Settings) -> LLMProvider:
    """Create the LLM provider based on settings."""
    if settings.llm_provider == "groq":
        from opsmesh.adapters.llm.groq import GroqProvider
        if not settings.llm_api_key:
            raise ValueError("OPSMESH_LLM_PROVIDER=groq requires OPSMESH_LLM_API_KEY")
        return GroqProvider(
            api_key=settings.llm_api_key,
            model=settings.llm_default_model,
        )
    elif settings.llm_provider == "mock":
        return MockLLMProvider()
    else:
        raise ValueError(f"Unknown LLM provider: {settings.llm_provider}")


def create_research_provider(settings: Settings) -> ResearchProvider:
    """Create the research provider based on settings."""
    # Check for Exa-specific settings
    exa_api_key = getattr(settings, "exa_api_key", "")
    research_provider = getattr(settings, "research_provider", "mock")

    if research_provider == "exa" or exa_api_key:
        from opsmesh.adapters.research.exa import ExaResearchProvider
        if not exa_api_key:
            raise ValueError("Research provider 'exa' requires OPSMESH_EXA_API_KEY")
        return ExaResearchProvider(api_key=exa_api_key)
    else:
        return MockResearchProvider()


def create_email_provider(settings: Settings) -> EmailProvider:
    """Create the email provider based on settings."""
    if settings.email_provider == "resend":
        from opsmesh.adapters.email.resend import ResendEmailProvider
        if not settings.email_api_key:
            raise ValueError("OPSMESH_EMAIL_PROVIDER=resend requires OPSMESH_EMAIL_API_KEY")
        return ResendEmailProvider(api_key=settings.email_api_key)
    elif settings.email_provider == "mock":
        return MockEmailProvider()
    else:
        raise ValueError(f"Unknown email provider: {settings.email_provider}")


def create_crm_provider(settings: Settings) -> CRMProvider:
    """Create the CRM provider based on settings."""
    if settings.crm_provider == "hubspot":
        from opsmesh.adapters.crm.hubspot import HubSpotCRMProvider
        if not settings.crm_api_key:
            raise ValueError("OPSMESH_CRM_PROVIDER=hubspot requires OPSMESH_CRM_API_KEY")
        return HubSpotCRMProvider(api_key=settings.crm_api_key)
    elif settings.crm_provider == "mock":
        return MockCRMProvider()
    else:
        raise ValueError(f"Unknown CRM provider: {settings.crm_provider}")


def create_notifier(settings: Settings) -> Notifier:
    """Create the notifier (always mock for v1 — no real Slack/email notifier yet)."""
    return MockNotifier()


__all__ = [
    "create_llm_provider",
    "create_research_provider",
    "create_email_provider",
    "create_crm_provider",
    "create_notifier",
]
