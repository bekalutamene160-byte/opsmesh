"""
OPSMESH HubSpot CRM Provider — real CRM operations via HubSpot API.

Architecture:
  Implements the CRMProvider Protocol using httpx.
  HubSpot API:
    GET    /crm/v3/objects/contacts/{id} — lookup
    PATCH  /crm/v3/objects/contacts/{id} — update
    POST   /crm/v3/objects/contacts — create

  MockCRMProvider remains the default for CI. This adapter is used when
  OPSMESH_CRM_PROVIDER=hubspot + OPSMESH_HUBSPOT_API_KEY is set.

  HubSpot uses contacts (not "leads"). We map OPSMESH lead_id to HubSpot
  contact ID (format: L_<digits> → HubSpot contact ID).
"""

from __future__ import annotations

from typing import Optional

import httpx

from opsmesh.adapters.crm.base import CRMLead, CRMLeadCreate, CRMProvider, ToolError
from opsmesh.models import LeadSource, utcnow
from opsmesh.tools.schemas import CreateLeadResult, UpdateLeadStatusResult


HUBSPOT_BASE_URL = "https://api.hubapi.com"

# Map OPSMESH lead status → HubSpot lifecycle stage
STATUS_TO_HUBSPOT = {
    "new": "lead",
    "contacted": "customer",
    "qualified": "qualified_lead",
    "rejected": "other",
    "disqualified": "other",
    "needs_review": "lead",
}


class HubSpotCRMProvider:
    """
    HubSpot CRM provider. Implements CRMProvider Protocol.

    Uses httpx to call HubSpot's v3 API.
    HubSpot API key is passed via the Authorization header.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=HUBSPOT_BASE_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(10.0, connect=10.0),
            )
        return self._client

    async def get_lead(
        self, workspace_id: str, lead_id: str
    ) -> Optional[CRMLead]:
        """Fetch a contact from HubSpot by ID."""
        client = await self._get_client()

        # HubSpot uses numeric IDs; our lead_id format is L_<digits>
        # Extract the numeric part
        hubspot_id = lead_id.replace("L_", "")

        try:
            response = await client.get(
                f"/crm/v3/objects/contacts/{hubspot_id}",
                params={"properties": "email,firstname,lastname,company,industry"},
            )
        except Exception:
            return None

        if response.status_code == 404:
            return None

        if response.status_code >= 400:
            return None

        data = response.json()
        props = data.get("properties", {})

        return CRMLead(
            lead_id=lead_id,
            workspace_id=workspace_id,
            company=props.get("company", ""),
            contact_email=props.get("email", ""),
            contact_name=f"{props.get('firstname', '')} {props.get('lastname', '')}".strip() or None,
            industry=props.get("industry"),
            status=props.get("lifecyclestage", "new"),
            created_at=utcnow(),
            last_modified_at=utcnow(),
        )

    async def update_lead_status(
        self, workspace_id: str, lead_id: str, status: str
    ) -> UpdateLeadStatusResult:
        """Update a contact's lifecycle stage in HubSpot."""
        client = await self._get_client()
        hubspot_id = lead_id.replace("L_", "")

        hubspot_stage = STATUS_TO_HUBSPOT.get(status)
        if hubspot_stage is None:
            raise ToolError(
                code="invalid_status",
                message=f"Status '{status}' not in HubSpot mapping",
            )

        response = await client.patch(
            f"/crm/v3/objects/contacts/{hubspot_id}",
            json={
                "properties": {
                    "lifecyclestage": hubspot_stage,
                }
            },
        )

        if response.status_code == 404:
            raise ToolError(
                code="not_found",
                message=f"Contact {lead_id} not found in HubSpot",
            )

        if response.status_code >= 400:
            raise ToolError(
                code="provider_error",
                message=f"HubSpot error: {response.status_code} {response.text[:200]}",
            )

        # Get the previous status (we don't have it; return 'unknown')
        return UpdateLeadStatusResult(
            lead_id=lead_id,
            previous_status="unknown",
            new_status=status,
        )

    async def create_lead(
        self, args: CRMLeadCreate
    ) -> CreateLeadResult:
        """Create a new contact in HubSpot."""
        client = await self._get_client()

        # Split contact_name into first/last
        first_name = ""
        last_name = ""
        if args.contact_name:
            parts = args.contact_name.split(" ", 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ""

        payload = {
            "properties": {
                "email": str(args.contact_email),
                "firstname": first_name,
                "lastname": last_name,
                "company": args.company,
                "industry": args.industry or "",
            }
        }

        response = await client.post(
            "/crm/v3/objects/contacts",
            json=payload,
        )

        if response.status_code == 409:
            # Contact already exists (duplicate email)
            raise ToolError(
                code="duplicate",
                message=f"Contact with email {args.contact_email} already exists in HubSpot",
            )

        if response.status_code >= 400:
            raise ToolError(
                code="provider_error",
                message=f"HubSpot error: {response.status_code} {response.text[:200]}",
            )

        data = response.json()
        hubspot_id = data.get("id", "0")

        # Return in our format: L_<digits>
        return CreateLeadResult(
            lead_id=f"L_{hubspot_id}",
        )

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


__all__ = ["HubSpotCRMProvider"]
