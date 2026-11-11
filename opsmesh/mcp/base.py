"""
OPSMESH MCP (Model Context Protocol) — base classes.

ARCHITECTURE CORRECTION (Day 4 hardening):
  The original Day 4 had two defects:
    1. Result was NOT validated against the declared result_schema.
       A handler could return malformed data and it would propagate.
    2. Workspace was hardcoded to "default" in CRM server handlers.

  This version:
    1. Validates args BEFORE handler execution (existing).
    2. Validates result AFTER handler execution (NEW).
    3. Accepts a SecurityContext parameter propagated from the gateway,
       so MCP servers can scope operations to the authenticated workspace.

Architecture (ADR-005):
  Tools live in MCP servers, each holding its own scoped credentials. The
  agent (worker) NEVER sees provider credentials — it calls tools through
  the MCP client, which routes to the appropriate server.

Day 4 implementation: IN-PROCESS MCP.
  The MCP servers run in the same process as the worker (Day 6 may split
  them into separate containers for production). We simulate the MCP
  protocol with direct function calls — same interface, no network hop.

Credential isolation:
  Each server holds its own credentials (CRM_API_KEY, EMAIL_API_KEY) in env
  vars. The client never sees them. The agent never sees them. This is the
  least-privilege boundary (Correction: blast radius is one tool, not account).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Optional, Type

from pydantic import BaseModel, ValidationError

from opsmesh.security_context import SecurityContext


class MCPError(Exception):
    """Base class for MCP errors. Carries a stable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class ToolNotFoundError(MCPError):
    def __init__(self, tool_name: str):
        super().__init__("tool_not_found", f"Tool '{tool_name}' is not registered")


class InvalidResultError(MCPError):
    """Raised when a handler returns a result that doesn't match the
    declared result_schema. This is a CRITICAL check: it prevents malformed
    tool output from propagating to the agent."""

    def __init__(self, tool_name: str, detail: str):
        super().__init__(
            "invalid_result",
            f"Tool '{tool_name}' returned a result that doesn't match its schema: {detail}",
        )


class ToolRegistration:
    """A registered tool: name, args schema, result schema, handler."""

    def __init__(
        self,
        name: str,
        args_schema: Type[BaseModel],
        result_schema: Type[BaseModel],
        handler: Callable,
        description: str = "",
    ):
        self.name = name
        self.args_schema = args_schema
        self.result_schema = result_schema
        self.handler = handler
        self.description = description


class MCPServer(ABC):
    """
    Abstract base class for an MCP server.

    Each server registers tools and dispatches calls. Servers hold their
    own credentials (read from env at construction time).

    Handler signature:
        async def handler(args: ArgsModel, ctx: SecurityContext) -> ResultModel

    The SecurityContext is propagated from the ToolGateway (trusted source).
    Handlers use ctx.workspace_id to scope CRM/email operations — NEVER
    accept workspace from untrusted LLM-generated args.
    """

    def __init__(self, name: str, version: str = "1.0.0"):
        self.name = name
        self.version = version
        self._tools: dict[str, ToolRegistration] = {}

    def register_tool(
        self,
        name: str,
        args_schema: Type[BaseModel],
        result_schema: Type[BaseModel],
        handler: Callable,
        description: str = "",
    ) -> None:
        """Register a tool with the server.

        Rejects malformed registrations:
          - name must be non-empty
          - schemas must be BaseModel subclasses
          - handler must be callable
        """
        if not name or not isinstance(name, str):
            raise MCPError("invalid_registration", "Tool name must be a non-empty string")
        if not (isinstance(args_schema, type) and issubclass(args_schema, BaseModel)):
            raise MCPError("invalid_registration", f"args_schema for '{name}' must be a BaseModel subclass")
        if not (isinstance(result_schema, type) and issubclass(result_schema, BaseModel)):
            raise MCPError("invalid_registration", f"result_schema for '{name}' must be a BaseModel subclass")
        if not callable(handler):
            raise MCPError("invalid_registration", f"handler for '{name}' must be callable")

        self._tools[name] = ToolRegistration(
            name=name,
            args_schema=args_schema,
            result_schema=result_schema,
            handler=handler,
            description=description,
        )

    def list_tools(self) -> list[str]:
        """Return the names of all registered tools."""
        return list(self._tools.keys())

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        ctx: Optional[SecurityContext] = None,
    ) -> Any:
        """
        Dispatch a tool call with full validation.

        Steps:
            1. Look up the tool by name (raise ToolNotFoundError if missing).
            2. Validate args against the tool's Pydantic schema.
            3. Call the handler with (validated_args, ctx).
            4. Validate the RESULT against the result_schema (NEW).
            5. Return the validated result.

        The SecurityContext (ctx) carries the trusted workspace_id from
        the ToolGateway. Handlers MUST use ctx.workspace_id, not args.

        Raises:
            ToolNotFoundError: unknown tool name
            MCPError("invalid_args"): args don't match the schema
            InvalidResultError: result doesn't match the result_schema
        """
        reg = self._tools.get(name)
        if reg is None:
            raise ToolNotFoundError(name)

        # 1. Validate args against the schema
        try:
            validated = reg.args_schema(**args)
        except ValidationError as e:
            raise MCPError("invalid_args", f"Args validation failed: {e}")

        # 2. Call the handler
        import asyncio
        if asyncio.iscoroutinefunction(reg.handler):
            result = await reg.handler(validated, ctx)
        else:
            result = reg.handler(validated, ctx)

        # 3. Validate the result against the result_schema (NEW)
        # This prevents malformed tool output from propagating to the agent.
        if result is None:
            raise InvalidResultError(name, "handler returned None")

        if isinstance(result, BaseModel):
            # Already a BaseModel — check it's the right type
            if not isinstance(result, reg.result_schema):
                raise InvalidResultError(
                    name,
                    f"expected {reg.result_schema.__name__}, got {type(result).__name__}"
                )
        elif isinstance(result, dict):
            # Dict — try to parse into the result schema
            try:
                result = reg.result_schema(**result)
            except ValidationError as e:
                raise InvalidResultError(name, str(e))
        else:
            raise InvalidResultError(
                name,
                f"expected {reg.result_schema.__name__} or dict, got {type(result).__name__}"
            )

        return result


class MCPClient:
    """
    The MCP client — routes tool calls to the appropriate server.

    The agent (via the ToolGateway) calls the client, which looks up
    which server hosts the tool and dispatches the call. The client
    NEVER holds credentials — only the servers do.

    SecurityContext propagation:
        The client passes the SecurityContext from the gateway to the
        server. This is the trusted workspace identity — servers use it
        to scope their operations. NEVER accept workspace from LLM args.
    """

    def __init__(self):
        self._servers: dict[str, MCPServer] = {}
        # Reverse index: tool_name -> server_name
        self._tool_index: dict[str, str] = {}

    def register_server(self, server: MCPServer) -> None:
        """Register an MCP server with the client."""
        self._servers[server.name] = server
        for tool_name in server.list_tools():
            self._tool_index[tool_name] = server.name

    def list_all_tools(self) -> list[str]:
        """List all tools across all servers."""
        return list(self._tool_index.keys())

    def find_server_for_tool(self, tool_name: str) -> Optional[MCPServer]:
        """Find which server hosts a given tool."""
        server_name = self._tool_index.get(tool_name)
        if server_name is None:
            return None
        return self._servers.get(server_name)

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        ctx: Optional[SecurityContext] = None,
    ) -> Any:
        """
        Route a tool call to the appropriate server.

        Passes the SecurityContext to the server for workspace scoping.

        Raises ToolNotFoundError if no server hosts this tool.
        """
        server = self.find_server_for_tool(tool_name)
        if server is None:
            raise ToolNotFoundError(tool_name)
        return await server.call_tool(tool_name, args, ctx)


__all__ = [
    "MCPError",
    "ToolNotFoundError",
    "InvalidResultError",
    "ToolRegistration",
    "MCPServer",
    "MCPClient",
]
