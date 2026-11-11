"""
MockCRMProvider — in-memory CRM for tests and local dev.

Architecture (ADR-008): mock-first. This provider uses an in-memory dict
keyed by (workspace_id, lead_id). It enforces workspace isolation, status
allowlists, and not_found errors — same contract as the real adapters.

Day 2 default behavior: starts empty; tests can pre-populate via add_lead().
"""

from __future__ import annotations

from typing import Optional

from opsmesh.adapters.crm.base import CRMLead, CRMLeadCreate, CRMProvider, ToolError
from opsmesh.models import utcnow
from opsmesh.tools.schemas import (
    CreateLeadResult,
    LookupLeadResult,
    UpdateLeadStatusResult,
)

# Allowlist of valid lead statuses (Engineering Contract §3).
# Defense in depth: even if the LLM tries status='admin', this rejects it.
ALLOWED_STATUSES = {
    "new", "contacted", "qualified", "rejected",
    "disqualified", "needs_review",
}


class MockCRMProvider:
    """
    In-memory CRM provider. Dict keyed by (workspace_id, lead_id).

    Usage:
        crm = MockCRMProvider()
        crm.add_lead(CRMLead(lead_id="L_001", workspace_id="ws_1", ...))
        lead = await crm.get_lead("ws_1", "L_001")
    """

    def __init__(self) -> None:
        # Key: (workspace_id, lead_id) → CRMLead
        self._leads: dict[tuple[str, str], CRMLead] = {}
        self._call_count = 0

    def add_lead(self, lead: CRMLead) -> None:
        """Pre-populate a lead (for tests / seed data)."""
        self._leads[(lead.workspace_id, lead.lead_id)] = lead

    @property
    def call_count(self) -> int:
        return self._call_count

    async def get_lead(
        self, workspace_id: str, lead_id: str
    ) -> Optional[CRMLead]:
        """Fetch a lead by ID. Returns None if not found OR cross-workspace."""
        self._call_count += 1
        return self._leads.get((workspace_id, lead_id))

    async def update_lead_status(
        self, workspace_id: str, lead_id: str, status: str
    ) -> UpdateLeadStatusResult:
        self._call_count += 1

        if status not in ALLOWED_STATUSES:
            raise ToolError(
                code="invalid_status",
                message=f"Status '{status}' not in allowlist {sorted(ALLOWED_STATUSES)}",
            )

        lead = self._leads.get((workspace_id, lead_id))
        if lead is None:
            raise ToolError(
                code="not_found",
                message=f"Lead {lead_id} not found in workspace {workspace_id}",
            )

        previous = lead.status
        lead.status = status
        lead.last_modified_at = utcnow()
        self._leads[(workspace_id, lead_id)] = lead

        return UpdateLeadStatusResult(
            lead_id=lead_id,
            previous_status=previous,
            new_status=status,
        )

    async def create_lead(
        self, args: CRMLeadCreate
    ) -> CreateLeadResult:
        self._call_count += 1

        # Generate a sequential-ish lead_id
        new_id = f"L_{len(self._leads) + 1:06d}"

        lead = CRMLead(
            lead_id=new_id,
            workspace_id=args.workspace_id,
            company=args.company,
            contact_email=args.contact_email,
            contact_name=args.contact_name,
            industry=args.industry,
            employees=args.employees,
            website=str(args.website) if args.website else None,
            status="new",
        )
        self._leads[(args.workspace_id, new_id)] = lead

        return CreateLeadResult(lead_id=new_id)


__all__ = ["MockCRMProvider", "ALLOWED_STATUSES"]
