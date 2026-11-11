"""
OPSMESH Comms MCP Server — hosts 2 communication tools.

ARCHITECTURE CORRECTION (Day 4 hardening):
  1. Accepts SecurityContext (no more hardcoded workspace).
  2. send_email performs a CONTACT SUPPRESSION check BEFORE the side effect.
     If the recipient is on the workspace suppression list, the call is
     rejected with MCPError("suppressed") — even if Gate 2 approved it.

  This is the defense-in-depth layer: Gate 2 approves the email, but the
  suppression check is a SEPARATE system-owned policy that human approval
  cannot override (Correction 7).

Tools hosted:
  1. send_email           (NOTIFY — ALWAYS requires Gate 2 approval)
  2. send_notification    (NOTIFY — internal only, does NOT require Gate 2)

The send_email tool requires an approval_id argument. The ToolGateway
verifies the approval record (existence, workspace, run, action, payload_hash,
expiration, consumption) BEFORE allowing the call to reach this server.

Ambiguous timeout handling (Correction 3):
  If the email adapter raises asyncio.TimeoutError, the supervisor catches
  it and transitions the run to SEND_UNKNOWN — NOT to FAILED. The reconciler
  later resolves the ambiguity.
"""

from __future__ import annotations

from typing import Optional

from opsmesh.adapters.email.base import EmailMessage, EmailProvider
from opsmesh.adapters.email.mock import MockEmailProvider
from opsmesh.adapters.notifier.base import Notifier
from opsmesh.adapters.notifier.mock import MockNotifier
from opsmesh.mcp.base import MCPServer, MCPError
from opsmesh.security_context import SecurityContext
from opsmesh.tools.schemas import (
    SendEmailArgs,
    SendEmailResult,
    SendNotificationArgs,
    SendNotificationResult,
)


class CommsMCPServer(MCPServer):
    """
    Comms MCP server — hosts send_email and send_notification.

    Credentials: holds the email adapter and notifier (which read
    EMAIL_API_KEY from env). The agent never sees them.

    Suppression: send_email checks the suppression list (provided at
    construction) BEFORE the side effect. Human approval does NOT override.
    """

    def __init__(
        self,
        email_provider: Optional[EmailProvider] = None,
        notifier: Optional[Notifier] = None,
        suppression_list: Optional[set[str]] = None,
    ):
        super().__init__(name="opsmesh-comms", version="1.0.0")
        self.email = email_provider or MockEmailProvider()
        self.notifier = notifier or MockNotifier()
        # Global suppression list (applies to all workspaces; Day 5 makes
        # this per-workspace via the ContactPolicy model).
        self._suppression = {e.lower() for e in (suppression_list or [])}
        self._register_tools()

    def add_suppressed_email(self, email: str) -> None:
        """Add an email to the suppression list (for tests / dynamic updates)."""
        self._suppression.add(email.lower())

    def _is_suppressed(self, email: str) -> bool:
        """Check if an email is suppressed. Case-insensitive."""
        return email.lower() in self._suppression

    def _register_tools(self) -> None:
        """Register both Comms tools."""

        # 1. send_email (NOTIFY — always requires Gate 2)
        self.register_tool(
            name="send_email",
            args_schema=SendEmailArgs,
            result_schema=SendEmailResult,
            handler=self._handle_send_email,
            description="Send an email. NOTIFY — always requires Gate 2 approval.",
        )

        # 2. send_notification (internal — no Gate 2)
        self.register_tool(
            name="send_notification",
            args_schema=SendNotificationArgs,
            result_schema=SendNotificationResult,
            handler=self._handle_send_notification,
            description="Send internal notification to workspace owner.",
        )

    async def _handle_send_email(
        self,
        args: SendEmailArgs,
        ctx: Optional[SecurityContext] = None,
    ) -> SendEmailResult:
        """Send an email via the email adapter.

        Steps:
            1. Require workspace (no hardcoded "default").
            2. CHECK SUPPRESSION LIST — reject if recipient is suppressed.
               Human approval does NOT override suppression (Correction 7).
            3. Construct EmailMessage and call the adapter.
            4. If the adapter raises asyncio.TimeoutError, it propagates.
               The supervisor transitions the run to SEND_UNKNOWN.
        """
        # 1. Require workspace from trusted context
        workspace_id = self._require_workspace(ctx)

        # 2. SUPPRESSION CHECK — defense-in-depth, NOT overridable by approval
        if self._is_suppressed(args.to):
            raise MCPError(
                "suppressed",
                f"Recipient {args.to} is on the suppression list. "
                "Human approval does not override contact policy.",
            )

        # 3. Send the email
        msg = EmailMessage(
            to=args.to,
            from_=args.from_,
            subject=args.subject,
            body=args.body,
            approval_id=args.approval_id,
        )
        result = await self.email.send(msg)
        return SendEmailResult(
            message_id=result.message_id,
            sent_at=result.sent_at,
            status=result.status,
        )

    async def _handle_send_notification(
        self,
        args: SendNotificationArgs,
        ctx: Optional[SecurityContext] = None,
    ) -> SendNotificationResult:
        """Send an internal notification (e.g., 'approval requested').

        No suppression check — notifications go to workspace_owner only,
        not external recipients."""
        result = await self.notifier.send(
            to=args.to,
            subject=args.subject,
            body=args.body,
            kind=args.kind,
        )
        return SendNotificationResult(
            notification_id=result.notification_id,
            sent_at=result.sent_at,
            channel=result.channel,
        )

    def _require_workspace(self, ctx: Optional[SecurityContext]) -> str:
        """Extract workspace_id from the SecurityContext. Raises if missing."""
        if ctx is None:
            raise MCPError(
                "forbidden",
                "No security context — workspace cannot be derived."
            )
        return ctx.workspace_id


__all__ = ["CommsMCPServer"]
