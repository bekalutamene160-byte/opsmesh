"""
OPSMESH CRM MCP Server — hosts 3 CRM tools.

ARCHITECTURE CORRECTION (Day 4 hardening):
  The original Day 4 had workspace_id="default" hardcoded in handlers.
  This version accepts a SecurityContext from the MCP client and uses
  ctx.workspace_id for ALL CRM operations. Cross-workspace access is
  blocked at the CRM adapter level (returns None / not_found).

  This means: Workspace A's LLM cannot read or modify Workspace B's leads.
  Even if the LLM is compromised and tries to forge a lead_id from
  another workspace, the CRM adapter returns None (cross-workspace → 404).

Tools hosted:
  1. lookup_lead          (READ — no approval required)
  2. update_lead_status   (WRITE — requires Gate 2 approval upstream)
  3. create_lead          (WRITE — requires Gate 1 approval when unknown company)

Handler signature (NEW):
    async def handler(args: ArgsModel, ctx: SecurityContext) -> ResultModel

The handler uses ctx.workspace_id — NEVER accepts workspace from args.
"""

from __future__ import annotations

from typing import Optional

from opsmesh.adapters.crm.base import CRMProvider, ToolError as CRMToolError
from opsmesh.adapters.crm.mock import MockCRMProvider
from opsmesh.mcp.base import MCPServer, MCPError
from opsmesh.security_context import SecurityContext
from opsmesh.tools.schemas import (
    CreateLeadArgs,
    CreateLeadResult,
    LookupLeadArgs,
    LookupLeadResult,
    UpdateLeadStatusArgs,
    UpdateLeadStatusResult,
)


class CRMMCPServer(MCPServer):
    """
    CRM MCP server — hosts lookup_lead, update_lead_status, create_lead.

    Credentials: holds the CRM adapter (which reads CRM_API_KEY from env).
    The agent never sees the adapter or the API key.

    Workspace isolation: uses ctx.workspace_id from the SecurityContext,
    propagated from the ToolGateway via the MCP client.
    """

    def __init__(self, crm_provider: Optional[CRMProvider] = None):
        super().__init__(name="opsmesh-crm", version="1.0.0")
        self.crm = crm_provider or MockCRMProvider()
        self._register_tools()

    def _register_tools(self) -> None:
        """Register all 3 CRM tools."""

        # 1. lookup_lead (READ)
        self.register_tool(
            name="lookup_lead",
            args_schema=LookupLeadArgs,
            result_schema=LookupLeadResult,
            handler=self._handle_lookup_lead,
            description="Fetch lead data from CRM. Read-only.",
        )

        # 2. update_lead_status (WRITE)
        self.register_tool(
            name="update_lead_status",
            args_schema=UpdateLeadStatusArgs,
            result_schema=UpdateLeadStatusResult,
            handler=self._handle_update_lead_status,
            description="Update lead status. Write action — requires Gate 2 approval.",
        )

        # 3. create_lead (WRITE)
        self.register_tool(
            name="create_lead",
            args_schema=CreateLeadArgs,
            result_schema=CreateLeadResult,
            handler=self._handle_create_lead,
            description="Create a new lead in the CRM.",
        )

    # --- Tool handlers ---

    async def _handle_lookup_lead(
        self, args: LookupLeadArgs, ctx: Optional[SecurityContext] = None
    ) -> LookupLeadResult:
        """Look up a lead by ID.

        Uses ctx.workspace_id — NEVER accepts workspace from args. If ctx
        is missing (e.g., direct test call), raises MCPError("forbidden").
        """
        workspace_id = self._require_workspace(ctx)
        lead = await self.crm.get_lead(
            workspace_id=workspace_id,
            lead_id=args.lead_id,
        )
        if lead is None:
            raise MCPError("not_found", f"Lead {args.lead_id} not found in workspace {workspace_id}")
        return LookupLeadResult(
            lead_id=lead.lead_id,
            company=lead.company,
            contact_email=lead.contact_email,
            contact_name=lead.contact_name,
            industry=lead.industry,
            employees=lead.employees,
            website=lead.website,
            created_at=lead.created_at,
            last_modified_at=lead.last_modified_at,
        )

    async def _handle_update_lead_status(
        self,
        args: UpdateLeadStatusArgs,
        ctx: Optional[SecurityContext] = None,
    ) -> UpdateLeadStatusResult:
        """Update a lead's status.

        The ToolGateway checks approval BEFORE this runs. Uses ctx.workspace_id.
        """
        workspace_id = self._require_workspace(ctx)
        try:
            return await self.crm.update_lead_status(
                workspace_id=workspace_id,
                lead_id=args.lead_id,
                status=args.status,
            )
        except CRMToolError as e:
            raise MCPError(e.code, e.message)

    async def _handle_create_lead(
        self,
        args: CreateLeadArgs,
        ctx: Optional[SecurityContext] = None,
    ) -> CreateLeadResult:
        """Create a new lead in the CRM. Uses ctx.workspace_id."""
        from opsmesh.adapters.crm.base import CRMLeadCreate
        from opsmesh.models import LeadSource

        workspace_id = self._require_workspace(ctx)
        create_args = CRMLeadCreate(
            workspace_id=workspace_id,
            company=args.company,
            contact_email=args.contact_email,
            contact_name=args.contact_name,
            industry=args.industry,
            employees=args.employees,
            website=str(args.website) if args.website else None,
            source=LeadSource(args.source),
        )
        return await self.crm.create_lead(create_args)

    def _require_workspace(self, ctx: Optional[SecurityContext]) -> str:
        """Extract workspace_id from the SecurityContext. Raises if missing.

        CRITICAL: workspace_id is NEVER accepted from untrusted LLM args.
        It comes from the authenticated session (via the ToolGateway →
        MCPClient → SecurityContext)."""
        if ctx is None:
            raise MCPError(
                "forbidden",
                "No security context — workspace cannot be derived. "
                "Direct calls without authentication are rejected."
            )
        return ctx.workspace_id


__all__ = ["CRMMCPServer"]
