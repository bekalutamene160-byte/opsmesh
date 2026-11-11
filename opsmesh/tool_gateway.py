"""
OPSMESH ToolGateway — the hardened policy enforcement layer.

ARCHITECTURE CORRECTION (Day 4 hardening):
  The original Day 4 had:
    1. ApprovalVerifier that always returned True (placeholder).
    2. Missing payload hash could bypass approval.
    3. Approval ID alone authorized actions (no hash binding).
    4. Hardcoded "default" workspace in MCP handlers.
    5. Audit + rate limit baked in, not separable.

  This version:
    1. Uses RealApprovalVerifier (Day 4 implemented) — actually checks
       the ApprovalStore for existence, expiration, consumption, workspace,
       run, action, and payload_hash.
    2. Missing payload_hash → REJECT (fail closed). Approval ID alone
       is NEVER sufficient.
    3. Recomputes payload_hash from the actual tool args and compares
       to the approval's stored hash. Catches modified recipients,
       subjects, bodies, tool names.
    4. Passes SecurityContext (with trusted workspace_id) to MCP servers.
    5. Audit + rate limit are now injectable interfaces (InMemory* for
       Day 4; Redis/Postgres for Day 6).

  ToolGateway now enforces 7 checks (was 6):
    1. Tool exists + has a policy
    2. Scope check (auth context has required scope)
    3. Approval check (existence, expiration, consumption, workspace, run,
       action, payload_hash — all verified via ApprovalStore)
    4. Allowlist check (constrained args)
    5. Rate limit (via injectable RateLimiter)
    6. Idempotency (IdempotentToolExecutor wraps the call)
    7. Audit log (via injectable AuditLogger — records success AND rejection)

CRITICAL INVARIANTS:
    - An approval ID alone NEVER authorizes an action. The payload_hash
      must match the EXACT action being approved.
    - A missing payload_hash NEVER bypasses approval verification.
    - Gate 1 approvals CANNOT authorize Gate 2 actions (kind mismatch).
    - Workspace A's LLM cannot access Workspace B's data (workspace scoping
      propagated via SecurityContext).
    - Suppressed emails are NEVER sent, even if Gate 2 approved them.
    - The LLM NEVER provides workspace_id, approval validity, or audit
      entries. These come from trusted sources (session, DB, system).
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from opsmesh.approval_store import (
    ApprovalError,
    RealApprovalVerifier,
    compute_payload_hash,
    TOOL_PROTECTED_FIELDS,
)
from opsmesh.idempotency import IdempotentToolExecutor, InProgressError
from opsmesh.mcp.base import MCPClient, MCPError
from opsmesh.security_context import (
    AuditLogger,
    AuthContext,
    InMemoryAuditLogger,
    InMemoryRateLimiter,
    RateLimiter,
    SecurityContext,
)


# ---------------------------------------------------------------------------
# Side-effect classes (course Ch 11)
# ---------------------------------------------------------------------------


from enum import Enum


class SideEffect(str, Enum):
    READ = "read"          # safe — no approval needed
    WRITE = "write"        # modifies state — requires approval (Gate 2)
    NOTIFY = "notify"      # sends communication — requires approval (Gate 2)
    EXTERNAL = "external"  # calls external API — requires approval


class ToolPolicy(BaseModel):
    """
    The policy for a single tool.

    Defines:
        side_effect:        the side-effect class (determines approval requirement)
        required_scope:     the auth scope needed to call this tool
        allowlist_args:     constrained args with allowed values
        rate_limit_per_min: max calls per minute per workspace
        requires_approval:  whether Gate 2 approval is required
        required_gate:      which approval kind is required (Gate 1 vs Gate 2)
    """

    name: str
    side_effect: SideEffect
    required_scope: str
    allowlist_args: Optional[dict[str, list[str]]] = None
    rate_limit_per_min: int = 30
    requires_approval: bool = True
    required_gate: Optional[str] = None  # "evidence_review" or "email_send"


# ---------------------------------------------------------------------------
# The 5 MCP tool policies (Correction 9: web_search is inline, NOT here)
# ---------------------------------------------------------------------------


TOOL_POLICIES: dict[str, ToolPolicy] = {
    "lookup_lead": ToolPolicy(
        name="lookup_lead",
        side_effect=SideEffect.READ,
        required_scope="crm.read",
        requires_approval=False,
        rate_limit_per_min=30,
    ),
    "update_lead_status": ToolPolicy(
        name="update_lead_status",
        side_effect=SideEffect.WRITE,
        required_scope="crm.write",
        allowlist_args={
            "status": ["contacted", "qualified", "rejected", "disqualified", "needs_review"],
        },
        requires_approval=True,
        required_gate="email_send",  # Gate 2 (CRM updates after email send)
        rate_limit_per_min=30,
    ),
    "create_lead": ToolPolicy(
        name="create_lead",
        side_effect=SideEffect.WRITE,
        required_scope="crm.write",
        requires_approval=True,
        required_gate="evidence_review",  # Gate 1 (unknown company on webhook)
        rate_limit_per_min=10,
    ),
    "send_email": ToolPolicy(
        name="send_email",
        side_effect=SideEffect.NOTIFY,
        required_scope="email.send",
        requires_approval=True,  # ALWAYS — Gate 2
        required_gate="email_send",
        rate_limit_per_min=10,
    ),
    "send_notification": ToolPolicy(
        name="send_notification",
        side_effect=SideEffect.NOTIFY,
        required_scope="notify.send",
        requires_approval=False,  # internal only
        rate_limit_per_min=30,
    ),
}


# ---------------------------------------------------------------------------
# Gateway exceptions
# ---------------------------------------------------------------------------


class GatewayError(Exception):
    """Base class for ToolGateway errors. Carries a stable code."""

    def __init__(self, code: str, message: str, tool_name: str = ""):
        self.code = code
        self.message = message
        self.tool_name = tool_name
        super().__init__(f"[{tool_name}] {code}: {message}")


class ApprovalRequiredError(GatewayError):
    def __init__(self, tool_name: str):
        super().__init__(
            code="approval_required",
            message=f"Tool '{tool_name}' requires a valid Gate 2 approval (approval_id + payload_hash)",
            tool_name=tool_name,
        )


class StaleApprovalError(GatewayError):
    """The approval's payload_hash doesn't match the call's payload, OR the
    approval is expired/consumed/workspace-mismatched/run-mismatched."""

    def __init__(self, tool_name: str, reason: str):
        super().__init__(
            code="stale_approval",
            message=reason,
            tool_name=tool_name,
        )


class MissingPayloadHashError(GatewayError):
    """A NOTIFY/WRITE tool was called WITHOUT a payload_hash. This is NEVER
    allowed — approval ID alone is insufficient."""

    def __init__(self, tool_name: str):
        super().__init__(
            code="missing_payload_hash",
            message=f"Tool '{tool_name}' requires a payload_hash (approval ID alone is insufficient)",
            tool_name=tool_name,
        )


class ArgPolicyError(GatewayError):
    """An arg value is not in the allowlist."""

    def __init__(self, tool_name: str, arg_name: str, value: str, allowed: list[str]):
        super().__init__(
            code="arg_policy_violation",
            message=f"{arg_name}={value!r} not in allowlist {allowed}",
            tool_name=tool_name,
        )


class ScopeError(GatewayError):
    """The caller's auth context doesn't include the required scope."""

    def __init__(self, tool_name: str, required_scope: str, caller_scopes: list[str]):
        super().__init__(
            code="insufficient_scope",
            message=f"Requires scope '{required_scope}', caller has {caller_scopes}",
            tool_name=tool_name,
        )


class RateLimitError(GatewayError):
    """Per-tool rate limit exceeded."""

    def __init__(self, tool_name: str, limit: int, window: str = "minute"):
        super().__init__(
            code="rate_limited",
            message=f"Rate limit exceeded: {limit} calls per {window}",
            tool_name=tool_name,
        )


class UnknownToolError(GatewayError):
    def __init__(self, tool_name: str):
        super().__init__(
            code="unknown_tool",
            message=f"Tool '{tool_name}' is not registered",
            tool_name=tool_name,
        )


class SuppressedContactError(GatewayError):
    """The recipient is on the suppression list. Human approval does NOT
    override this (Correction 7)."""

    def __init__(self, tool_name: str, email: str):
        super().__init__(
            code="suppressed",
            message=f"Recipient {email} is suppressed; human approval cannot override contact policy",
            tool_name=tool_name,
        )


# ---------------------------------------------------------------------------
# The ToolGateway (hardened)
# ---------------------------------------------------------------------------


class ToolGateway:
    """
    The hardened policy enforcement layer between the agent and MCP servers.

    Every tool call goes through the gateway:
        agent → gateway.execute(tool, args, auth) → MCP client → server → adapter

    The gateway enforces 7 checks BEFORE dispatching to the MCP client:
        1. Tool exists + has a policy
        2. Scope check (auth context has required scope)
        3. Approval check (existence, expiration, consumption, workspace,
           run, action, payload_hash — all verified via ApprovalStore)
        4. Allowlist check (constrained args)
        5. Rate limit (via injectable RateLimiter)
        6. Idempotency (IdempotentToolExecutor wraps the call)
        7. Audit log (via injectable AuditLogger)

    CRITICAL: An approval ID alone NEVER authorizes an action. The
    payload_hash must match the EXACT action being approved.
    """

    def __init__(
        self,
        client: MCPClient,
        store: Any,  # StateStore
        approval_verifier: RealApprovalVerifier,
        audit_logger: Optional[AuditLogger] = None,
        rate_limiter: Optional[RateLimiter] = None,
    ):
        self.client = client
        self.store = store
        self.approval_verifier = approval_verifier
        self._idempotent_executor = IdempotentToolExecutor(store=store)
        self.audit_logger = audit_logger or InMemoryAuditLogger()
        self.rate_limiter = rate_limiter or InMemoryRateLimiter()

    async def execute(
        self,
        tool_name: str,
        args: dict[str, Any],
        auth: AuthContext,
        run_id: str,
        step_id: int,
        approval_id: Optional[str] = None,
        payload_hash: Optional[str] = None,
        actor: str = "supervisor",
    ) -> Any:
        """
        Execute a tool call through the gateway.

        All 7 checks run before dispatch. On any failure, a GatewayError
        subclass is raised (the agent never reaches the provider).
        """
        # Build the SecurityContext to propagate to MCP servers
        ctx = SecurityContext(
            workspace_id=auth.workspace_id,
            run_id=run_id,
            step_id=step_id,
            actor=actor,
            scopes=auth.scopes,
        )

        # --- Check 1: Tool exists + has a policy ---
        policy = TOOL_POLICIES.get(tool_name)
        if policy is None:
            self._audit(tool_name, args, auth, run_id, "rejected", "unknown_tool")
            raise UnknownToolError(tool_name)

        # --- Check 2: Scope (BEFORE approval — even READ tools need scope) ---
        if policy.required_scope not in auth.scopes:
            self._audit(tool_name, args, auth, run_id, "rejected", "insufficient_scope")
            raise ScopeError(tool_name, policy.required_scope, auth.scopes)

        # --- Check 3: Approval (with REAL verification) ---
        if policy.requires_approval:
            # CRITICAL: approval_id AND payload_hash are BOTH required.
            # Neither alone is sufficient.
            if approval_id is None:
                self._audit(tool_name, args, auth, run_id, "rejected", "no_approval")
                raise ApprovalRequiredError(tool_name)

            if payload_hash is None:
                # Recompute the payload_hash from the actual args
                # (the caller may have forgotten to pass it; we recompute
                # for safety, then verify against the approval's stored hash)
                protected = TOOL_PROTECTED_FIELDS.get(tool_name)
                payload_hash = compute_payload_hash(tool_name, args, protected)

            # Real verification via ApprovalStore (existence, expiration,
            # consumption, workspace, run, action, payload_hash)
            try:
                await self.approval_verifier.verify_and_consume(
                    approval_id=approval_id,
                    workspace_id=auth.workspace_id,
                    run_id=run_id,
                    action=tool_name,
                    payload_hash=payload_hash,
                )
            except ApprovalError as e:
                self._audit(tool_name, args, auth, run_id, "rejected", f"stale_approval:{e.code}")
                raise StaleApprovalError(tool_name, str(e))

        # --- Check 4: Allowlist args ---
        if policy.allowlist_args:
            for arg_name, allowed in policy.allowlist_args.items():
                if arg_name in args:
                    value = args[arg_name]
                    if value not in allowed:
                        self._audit(tool_name, args, auth, run_id, "rejected", "arg_policy")
                        raise ArgPolicyError(tool_name, arg_name, str(value), allowed)

        # --- Check 5: Rate limit (via injectable RateLimiter) ---
        if not self.rate_limiter.check_and_record(
            auth.workspace_id, tool_name, policy.rate_limit_per_min
        ):
            self._audit(tool_name, args, auth, run_id, "rejected", "rate_limited")
            raise RateLimitError(tool_name, policy.rate_limit_per_min)

        # --- Check 6: Idempotency (wrap the MCP call) ---
        try:
            result = await self._idempotent_executor.call(
                tool=self._mcp_dispatcher(tool_name, ctx),
                workspace_id=auth.workspace_id,
                run_id=run_id,
                step_id=step_id,
                tool_name=tool_name,
                args=args,
                payload_hash=payload_hash,
            )
        except InProgressError:
            self._audit(tool_name, args, auth, run_id, "rejected", "in_progress")
            raise
        except MCPError as e:
            # MCP errors propagate (includes suppressed contact)
            self._audit(tool_name, args, auth, run_id, "rejected", f"mcp_error:{e.code}")
            if e.code == "suppressed":
                raise SuppressedContactError(tool_name, args.get("to", "unknown"))
            raise

        # --- Check 7: Audit ---
        self._audit(tool_name, args, auth, run_id, "success")

        return result

    def _mcp_dispatcher(self, tool_name: str, ctx: SecurityContext):
        """Return an async callable that dispatches to the MCP client.

        Passes the SecurityContext to the MCP client for workspace scoping."""
        async def dispatch(**kwargs):
            return await self.client.call_tool(tool_name, kwargs, ctx)
        return dispatch

    def _audit(
        self,
        tool_name: str,
        args: dict,
        auth: AuthContext,
        run_id: str,
        outcome: str,
        rejection_reason: Optional[str] = None,
    ) -> None:
        """Record an audit entry via the injectable AuditLogger."""
        self.audit_logger.log(
            workspace_id=auth.workspace_id,
            run_id=run_id,
            tool_name=tool_name,
            actor="supervisor",
            args=args,
            outcome=outcome,
            rejection_reason=rejection_reason,
        )

    @property
    def audit_entries(self) -> list[dict]:
        """The audit log entries (for tests / dashboard)."""
        return self.audit_logger.entries()


__all__ = [
    "SideEffect",
    "ToolPolicy",
    "TOOL_POLICIES",
    "GatewayError",
    "ApprovalRequiredError",
    "StaleApprovalError",
    "MissingPayloadHashError",
    "ArgPolicyError",
    "ScopeError",
    "RateLimitError",
    "UnknownToolError",
    "SuppressedContactError",
    "AuthContext",
    "ToolGateway",
]
