"""
OPSMESH Integrations API — provider health check.

Endpoint:
  GET /api/v1/integrations — returns the status of all external providers

Architecture:
  Reports which providers are configured (mock or real) and whether
  they have API keys set. Does NOT make real API calls — just reports
  configuration status.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from opsmesh.api.auth import get_current_user
from opsmesh.config import get_settings


router = APIRouter(prefix="/api/v1", tags=["integrations"])


@router.get("/integrations")
async def get_integrations(user: dict = Depends(get_current_user)):
    """Get the status of all external provider integrations.

    Returns which providers are configured and whether they have API keys.
    Does NOT make real API calls — this is a configuration check.
    """
    settings = get_settings()

    return {
        "llm": {
            "provider": settings.llm_provider,
            "model": settings.llm_default_model,
            "api_key_configured": bool(settings.llm_api_key),
            "status": "configured" if settings.llm_api_key or settings.llm_provider == "mock" else "missing_key",
        },
        "research": {
            "provider": getattr(settings, "research_provider", "mock"),
            "api_key_configured": bool(getattr(settings, "exa_api_key", "")),
            "status": "configured" if getattr(settings, "exa_api_key", "") or getattr(settings, "research_provider", "mock") == "mock" else "missing_key",
        },
        "email": {
            "provider": settings.email_provider,
            "api_key_configured": bool(settings.email_api_key),
            "kill_switch": settings.email_kill_switch,
            "status": "configured" if settings.email_api_key or settings.email_provider == "mock" else "missing_key",
        },
        "crm": {
            "provider": settings.crm_provider,
            "api_key_configured": bool(settings.crm_api_key),
            "status": "configured" if settings.crm_api_key or settings.crm_provider == "mock" else "missing_key",
        },
        "database": {
            "type": "sqlite" if settings.database_path else "in_memory",
            "path": settings.database_path or "(in-memory)",
            "production_mode": settings.production_mode,
        },
    }


__all__ = ["router"]
