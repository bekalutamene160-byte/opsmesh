"""
CRM Provider Protocol — the interface for CRM adapters.

Architecture (ADR-008): mock-first. The MockCRMProvider is an in-memory
dict; real adapters (HubSpotCRMProvider, PipedriveCRMProvider) implement
the same Protocol and are selected via env var.

The CRM adapter is called by:
  - The Research specialist (lookup_lead) — READ, no approval.
  - The CRM outbox reconciler (update_lead_status, create_lead) — WRITE,
    gated by the ToolGateway (Day 4) which checks the approval record.

Workspace isolation: every method takes workspace_id. Real adapters pass
it as a header or query param; the mock enforces it via dict key scoping.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, EmailStr, Field

from opsmesh.models import LeadSource, utcnow
from opsmesh.tools.schemas import (
    CreateLeadArgs,
    CreateLeadResult,
    LookupLeadResult,
    UpdateLeadStatusResult,
)


class CRMLead(BaseModel):
    """A lead as stored in the external CRM. The internal OPSMESH Lead
    (opsmesh.models.Lead) is the source of truth; this is the sync target."""

    lead_id: str = Field(pattern=r"^L_\d+$")
    workspace_id: str
    company: str
    contact_email: EmailStr
    contact_name: Optional[str] = None
    industry: Optional[str] = None
    employees: Optional[int] = Field(default=None, ge=1)
    website: Optional[str] = None
    status: str = "new"
    created_at: datetime = Field(default_factory=utcnow)
    last_modified_at: datetime = Field(default_factory=utcnow)


class CRMLeadCreate(BaseModel):
    """Args for creating a lead in the CRM (mapped from CreateLeadArgs)."""

    workspace_id: str
    company: str
    contact_email: EmailStr
    contact_name: Optional[str] = None
    industry: Optional[str] = None
    employees: Optional[int] = Field(default=None, ge=1)
    website: Optional[str] = None
    source: LeadSource = LeadSource.MANUAL


class ToolError(Exception):
    """Raised when a CRM tool call fails. Carries a stable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@runtime_checkable
class CRMProvider(Protocol):
    """The CRM provider contract."""

    async def get_lead(
        self, workspace_id: str, lead_id: str
    ) -> Optional[CRMLead]:
        """Fetch a lead by ID. Returns None if not found OR if the lead
        belongs to a different workspace (cross-workspace → 404)."""
        ...

    async def update_lead_status(
        self, workspace_id: str, lead_id: str, status: str
    ) -> UpdateLeadStatusResult:
        """Update a lead's status. Raises ToolError(code='invalid_status')
        if status is not in the allowlist, or ToolError(code='not_found')
        if the lead doesn't exist in this workspace."""
        ...

    async def create_lead(
        self, args: CRMLeadCreate
    ) -> CreateLeadResult:
        """Create a new lead in the CRM. Returns the new lead_id."""
        ...


__all__ = ["CRMLead", "CRMLeadCreate", "CRMProvider", "ToolError"]
