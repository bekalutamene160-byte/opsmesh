"""
Tests for opsmesh.mcp — the MCP servers and client (with SecurityContext).

ARCHITECTURE CORRECTION (Day 4 hardening):
  Handlers now accept a SecurityContext parameter for workspace scoping.
  No more hardcoded "default" workspace.

Covers:
  - CRMMCPServer registers 3 tools
  - CommsMCPServer registers 2 tools
  - MCPClient routes calls to the correct server
  - ToolNotFoundError for unknown tools
  - Args validation (Pydantic schema enforcement)
  - RESULT validation (NEW — InvalidResultError for malformed output)
  - CRM tools work with SecurityContext (workspace scoping)
  - Comms tools work with SecurityContext
  - Missing SecurityContext → forbidden
  - Credential isolation: the client never sees provider credentials
"""

from __future__ import annotations

import pytest

from opsmesh.adapters.crm.base import CRMLead
from opsmesh.adapters.crm.mock import MockCRMProvider
from opsmesh.adapters.email.mock import MockEmailProvider
from opsmesh.adapters.notifier.mock import MockNotifier
from opsmesh.mcp.base import InvalidResultError, MCPClient, MCPError, ToolNotFoundError
from opsmesh.mcp.comms_server import CommsMCPServer
from opsmesh.mcp.crm_server import CRMMCPServer
from opsmesh.security_context import SecurityContext


@pytest.fixture
def ctx():
    """A SecurityContext for workspace ws_1."""
    return SecurityContext(
        workspace_id="ws_1", run_id="run_1", step_id=1, actor="supervisor",
        scopes=["crm.read", "crm.write", "email.send", "notify.send"],
    )


# ---------------------------------------------------------------------------
# 1. CRM MCP Server
# ---------------------------------------------------------------------------


class TestCRMMCPServer:
    def test_registers_3_tools(self):
        server = CRMMCPServer()
        tools = server.list_tools()
        assert "lookup_lead" in tools
        assert "update_lead_status" in tools
        assert "create_lead" in tools
        assert len(tools) == 3

    @pytest.mark.asyncio
    async def test_lookup_lead_returns_lead(self, ctx):
        crm = MockCRMProvider()
        crm.add_lead(CRMLead(
            lead_id="L_001", workspace_id="ws_1",
            company="Acme", contact_email="r@acme.com",
        ))
        server = CRMMCPServer(crm_provider=crm)
        result = await server.call_tool("lookup_lead", {"lead_id": "L_001"}, ctx)
        assert result.company == "Acme"

    @pytest.mark.asyncio
    async def test_lookup_lead_not_found_raises(self, ctx):
        server = CRMMCPServer()
        with pytest.raises(MCPError) as exc_info:
            await server.call_tool("lookup_lead", {"lead_id": "L_999"}, ctx)
        assert exc_info.value.code == "not_found"

    @pytest.mark.asyncio
    async def test_update_lead_status_valid(self, ctx):
        crm = MockCRMProvider()
        crm.add_lead(CRMLead(
            lead_id="L_001", workspace_id="ws_1",
            company="Acme", contact_email="r@acme.com",
        ))
        server = CRMMCPServer(crm_provider=crm)
        result = await server.call_tool("update_lead_status", {
            "lead_id": "L_001", "status": "contacted",
        }, ctx)
        assert result.new_status == "contacted"

    @pytest.mark.asyncio
    async def test_update_lead_status_invalid_rejected(self, ctx):
        """The Pydantic schema rejects status='admin' before the CRM adapter sees it."""
        server = CRMMCPServer()
        with pytest.raises(MCPError) as exc_info:
            await server.call_tool("update_lead_status", {
                "lead_id": "L_001", "status": "admin",
            }, ctx)
        assert exc_info.value.code == "invalid_args"

    @pytest.mark.asyncio
    async def test_create_lead(self, ctx):
        server = CRMMCPServer()
        result = await server.call_tool("create_lead", {
            "company": "Globex",
            "contact_email": "founder@globex.com",
        }, ctx)
        assert result.lead_id.startswith("L_")

    @pytest.mark.asyncio
    async def test_missing_ctx_raises_forbidden(self):
        """Without a SecurityContext, the server rejects the call (no hardcoded 'default')."""
        server = CRMMCPServer()
        with pytest.raises(MCPError) as exc_info:
            await server.call_tool("lookup_lead", {"lead_id": "L_001"}, ctx=None)
        assert exc_info.value.code == "forbidden"

    @pytest.mark.asyncio
    async def test_cross_workspace_lookup_returns_not_found(self, ctx):
        """Workspace A's LLM cannot see Workspace B's leads."""
        crm = MockCRMProvider()
        # Add a lead in ws_2
        crm.add_lead(CRMLead(
            lead_id="L_002", workspace_id="ws_2",
            company="Globex", contact_email="r@globex.com",
        ))
        server = CRMMCPServer(crm_provider=crm)
        # Try to look it up from ws_1 (ctx.workspace_id="ws_1")
        with pytest.raises(MCPError) as exc_info:
            await server.call_tool("lookup_lead", {"lead_id": "L_002"}, ctx)
        assert exc_info.value.code == "not_found"


# ---------------------------------------------------------------------------
# 2. Comms MCP Server
# ---------------------------------------------------------------------------


class TestCommsMCPServer:
    def test_registers_2_tools(self):
        server = CommsMCPServer()
        tools = server.list_tools()
        assert "send_email" in tools
        assert "send_notification" in tools
        assert len(tools) == 2

    @pytest.mark.asyncio
    async def test_send_email_captures_message(self, ctx):
        email = MockEmailProvider()
        server = CommsMCPServer(email_provider=email)
        result = await server.call_tool("send_email", {
            "to": "founder@acme.com",
            "from_": "bekalu@opsmesh.dev",
            "subject": "Hello",
            "body": "Body text",
            "approval_id": "ap_abcdef123456",
        }, ctx)
        assert result.status == "sent"
        assert len(email.sent_messages) == 1

    @pytest.mark.asyncio
    async def test_send_email_invalid_approval_id_rejected(self, ctx):
        """approval_id must match ap_[a-f0-9]{12}."""
        server = CommsMCPServer()
        with pytest.raises(MCPError) as exc_info:
            await server.call_tool("send_email", {
                "to": "r@acme.com",
                "from_": "s@x.com",
                "subject": "x",
                "body": "x",
                "approval_id": "bad_id",
            }, ctx)
        assert exc_info.value.code == "invalid_args"

    @pytest.mark.asyncio
    async def test_send_notification(self, ctx):
        notifier = MockNotifier()
        server = CommsMCPServer(notifier=notifier)
        result = await server.call_tool("send_notification", {
            "subject": "Approval requested",
            "body": "Please review run X",
            "kind": "approval_requested",
        }, ctx)
        assert result.channel == "email"
        assert len(notifier.sent) == 1

    @pytest.mark.asyncio
    async def test_send_email_missing_ctx_raises(self):
        server = CommsMCPServer()
        with pytest.raises(MCPError) as exc_info:
            await server.call_tool("send_email", {
                "to": "r@acme.com", "from_": "s@x.com",
                "subject": "x", "body": "x",
                "approval_id": "ap_abcdef123456",
            }, ctx=None)
        assert exc_info.value.code == "forbidden"

    @pytest.mark.asyncio
    async def test_suppressed_email_rejected(self, ctx):
        """The Comms server checks the suppression list before sending."""
        email = MockEmailProvider()
        server = CommsMCPServer(
            email_provider=email,
            suppression_list={"evil@attacker.com"},
        )
        with pytest.raises(MCPError) as exc_info:
            await server.call_tool("send_email", {
                "to": "evil@attacker.com",
                "from_": "s@x.com",
                "subject": "x", "body": "x",
                "approval_id": "ap_abcdef123456",
            }, ctx)
        assert exc_info.value.code == "suppressed"
        assert len(email.sent_messages) == 0  # not sent


# ---------------------------------------------------------------------------
# 3. MCP Client (routing)
# ---------------------------------------------------------------------------


class TestMCPClient:
    @pytest.mark.asyncio
    async def test_routes_to_correct_server(self):
        """The client routes tool calls to the correct server."""
        crm_server = CRMMCPServer()
        comms_server = CommsMCPServer()
        client = MCPClient()
        client.register_server(crm_server)
        client.register_server(comms_server)

        assert client.find_server_for_tool("lookup_lead") is crm_server
        assert client.find_server_for_tool("send_email") is comms_server

    @pytest.mark.asyncio
    async def test_unknown_tool_raises(self):
        client = MCPClient()
        client.register_server(CRMMCPServer())
        with pytest.raises(ToolNotFoundError):
            await client.call_tool("nonexistent_tool", {})

    @pytest.mark.asyncio
    async def test_list_all_tools(self):
        client = MCPClient()
        client.register_server(CRMMCPServer())
        client.register_server(CommsMCPServer())
        all_tools = client.list_all_tools()
        assert len(all_tools) == 5

    @pytest.mark.asyncio
    async def test_credential_isolation(self):
        """The client NEVER sees provider credentials."""
        crm_server = CRMMCPServer()
        client = MCPClient()
        client.register_server(crm_server)
        assert not hasattr(client, "crm")
        assert hasattr(crm_server, "crm")  # server encapsulates the adapter

    @pytest.mark.asyncio
    async def test_client_passes_ctx_to_server(self, ctx):
        """The client propagates the SecurityContext to the server."""
        crm = MockCRMProvider()
        crm.add_lead(CRMLead(
            lead_id="L_001", workspace_id="ws_1",
            company="Acme", contact_email="r@acme.com",
        ))
        client = MCPClient()
        client.register_server(CRMMCPServer(crm_provider=crm))
        # Pass ctx — should work
        result = await client.call_tool("lookup_lead", {"lead_id": "L_001"}, ctx)
        assert result.company == "Acme"


# ---------------------------------------------------------------------------
# 4. Result validation (NEW)
# ---------------------------------------------------------------------------


class TestResultValidation:
    @pytest.mark.asyncio
    async def test_invalid_result_raises(self):
        """If a handler returns a dict that doesn't match result_schema,
        InvalidResultError is raised."""
        from opsmesh.mcp.base import MCPServer
        from opsmesh.tools.schemas import SendEmailResult
        from pydantic import BaseModel

        class EmptyArgs(BaseModel):
            pass

        class BadServer(MCPServer):
            def __init__(self):
                super().__init__(name="bad-server", version="1.0.0")
                self.register_tool(
                    name="bad_tool",
                    args_schema=EmptyArgs,
                    result_schema=SendEmailResult,
                    handler=self._bad_handler,
                )

            async def _bad_handler(self, args, ctx=None):
                return {"status": "invalid_status"}  # not a valid SendEmailResult

        server = BadServer()
        with pytest.raises(InvalidResultError):
            await server.call_tool("bad_tool", {})

    @pytest.mark.asyncio
    async def test_none_result_raises(self):
        from opsmesh.mcp.base import MCPServer
        from opsmesh.tools.schemas import SendEmailResult
        from pydantic import BaseModel

        class EmptyArgs(BaseModel):
            pass

        class NoneServer(MCPServer):
            def __init__(self):
                super().__init__(name="none-server", version="1.0.0")
                self.register_tool(
                    name="none_tool",
                    args_schema=EmptyArgs,
                    result_schema=SendEmailResult,
                    handler=self._none_handler,
                )

            async def _none_handler(self, args, ctx=None):
                return None

        server = NoneServer()
        with pytest.raises(InvalidResultError):
            await server.call_tool("none_tool", {})

    @pytest.mark.asyncio
    async def test_valid_result_passes(self, ctx):
        """A valid result (correct type) passes validation."""
        crm = MockCRMProvider()
        crm.add_lead(CRMLead(
            lead_id="L_001", workspace_id="ws_1",
            company="Acme", contact_email="r@acme.com",
        ))
        server = CRMMCPServer(crm_provider=crm)
        # lookup_lead returns a LookupLeadResult — should pass validation
        result = await server.call_tool("lookup_lead", {"lead_id": "L_001"}, ctx)
        assert result.company == "Acme"
