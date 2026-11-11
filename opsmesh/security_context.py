"""
OPSMESH Security context — workspace propagation + audit + rate limiter interfaces.

ARCHITECTURE CORRECTION (Day 4 hardening):
  The original Day 4 had:
    1. Hardcoded workspace_id="default" in MCP server handlers.
    2. ApprovalVerifier that always returned True.
    3. In-memory audit + rate limit baked into ToolGateway (not separable).

  This module provides:
    1. SecurityContext — trusted workspace identity propagated through the
       call chain. NEVER accepted from untrusted LLM-generated args.
    2. AuditLogger Protocol + InMemoryAuditLogger — separates the audit
       interface from its in-memory implementation. Day 6 will swap in a
       Postgres-backed logger that persists to `audit_events`.
    3. RateLimiter Protocol + InMemoryRateLimiter — same separation. Day 6
       will swap in a Redis-backed limiter for distributed enforcement.

TRUST BOUNDARIES:
    ┌─ UNTRUSTED ─────────────────────────────────────┐
    │ LLM-generated tool calls, args, recipient, body │
    └──────────────────────────────────────────────────┘
                          ↓ (passes through ToolGateway)
    ┌─ TRUSTED ────────────────────────────────────────┐
    │ AuthContext.workspace_id (from session/OAuth)    │
    │ ApprovalRecord (from ApprovalStore)             │
    │ AuditLog (system-owned, append-only)            │
    └──────────────────────────────────────────────────┘

  The LLM NEVER provides workspace_id, approval_id validity, or audit
  entries. These come from trusted sources (session, DB, system).
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Optional, Protocol


# ---------------------------------------------------------------------------
# AuthContext — moved here to break the circular import with tool_gateway
# ---------------------------------------------------------------------------


class AuthContext:
    """The caller's authentication context. Carries workspace + scopes.

    Day 4: simplified (just workspace_id + scopes list).
    Day 6: derived from the session cookie + OAuth.
    """

    def __init__(self, workspace_id: str, scopes: Optional[list[str]] = None):
        self.workspace_id = workspace_id
        # CRITICAL: use explicit None check, not `or` — empty list [] is falsy
        # and would incorrectly fall back to defaults. `scopes=[]` means NO scopes.
        if scopes is None:
            self.scopes = ["crm.read", "crm.write", "email.send", "notify.send", "web.search"]
        else:
            self.scopes = scopes


# ---------------------------------------------------------------------------
# SecurityContext — extends AuthContext with call-scoped data
# ---------------------------------------------------------------------------


class SecurityContext:
    """
    Trusted security context for a single tool call.

    Carries:
        - workspace_id (from authenticated session, NEVER from LLM args)
        - run_id (from the supervisor, NEVER from LLM args)
        - step_id (for audit trail)
        - actor (who initiated the call: "supervisor", "reconciler", user_id)
        - scopes (authorization scopes from the session)

    This is constructed by the ToolGateway from trusted sources and passed
    to MCP servers via the MCP client. The MCP servers use it to scope
    their CRM/email operations to the correct workspace.
    """

    def __init__(
        self,
        workspace_id: str,
        run_id: str,
        step_id: int,
        actor: str = "supervisor",
        scopes: Optional[list[str]] = None,
    ):
        self.workspace_id = workspace_id
        self.run_id = run_id
        self.step_id = step_id
        self.actor = actor
        self.scopes = scopes or []

    def to_auth_context(self) -> "AuthContext":
        """Convert to AuthContext for the ToolGateway's scope check."""
        return AuthContext(workspace_id=self.workspace_id, scopes=self.scopes)

    def __repr__(self) -> str:
        return (
            f"SecurityContext(ws={self.workspace_id}, run={self.run_id}, "
            f"step={self.step_id}, actor={self.actor})"
        )


# ---------------------------------------------------------------------------
# AuditLogger — Protocol + in-memory implementation
# ---------------------------------------------------------------------------


class AuditLogger(Protocol):
    """
    Audit logger contract.

    Records every tool call attempt — successful or rejected — with:
        - workspace_id, run_id, tool_name, actor
        - args (REDACTED for sensitive fields)
        - outcome: "success" | "rejected" | "error"
        - rejection_reason (if rejected)
        - timestamp

    Day 4: InMemoryAuditLogger (for tests/dev).
    Day 6: PostgresAuditLogger (persists to `audit_events` table).

    CRITICAL: Never log secrets or raw PII. The logger redacts sensitive
    fields (password, token, api_key) before recording.
    """

    def log(
        self,
        workspace_id: str,
        run_id: str,
        tool_name: str,
        actor: str,
        args: dict,
        outcome: str,
        rejection_reason: Optional[str] = None,
    ) -> None:
        """Record an audit entry."""
        ...

    def entries(self) -> list[dict]:
        """Return all audit entries (for tests / dashboard)."""
        ...


# Sensitive field substrings (case-insensitive) — redacted before logging
_SENSITIVE_LOG_SUBSTRINGS = ("password", "secret", "token", "api_key", "apikey")


def _redact_for_log(args: dict) -> dict:
    """Redact sensitive fields before logging. Returns a copy."""
    redacted = {}
    for key, value in args.items():
        if any(s in key.lower() for s in _SENSITIVE_LOG_SUBSTRINGS):
            redacted[key] = "[REDACTED]"
        else:
            redacted[key] = value
    return redacted


class InMemoryAuditLogger:
    """
    In-memory audit logger. For tests + local dev.

    NOT distributed-safe. For multi-process deployments, use
    PostgresAuditLogger (Day 6) which persists to the `audit_events` table.
    """

    def __init__(self):
        self._entries: list[dict] = []

    def log(
        self,
        workspace_id: str,
        run_id: str,
        tool_name: str,
        actor: str,
        args: dict,
        outcome: str,
        rejection_reason: Optional[str] = None,
    ) -> None:
        self._entries.append({
            "workspace_id": workspace_id,
            "run_id": run_id,
            "tool_name": tool_name,
            "actor": actor,
            "args": _redact_for_log(args),
            "outcome": outcome,
            "rejection_reason": rejection_reason,
            "timestamp": time.time(),
        })

    def entries(self) -> list[dict]:
        return self._entries.copy()

    def clear(self) -> None:
        self._entries.clear()


# ---------------------------------------------------------------------------
# RateLimiter — Protocol + in-memory implementation
# ---------------------------------------------------------------------------


class RateLimiter(Protocol):
    """
    Rate limiter contract.

    Enforces per-tool, per-workspace, per-minute rate limits.

    Day 4: InMemoryRateLimiter (single-process, asyncio-safe).
    Day 6: RedisRateLimiter (distributed, atomic via INCR + EXPIRE).

    CRITICAL: The in-memory implementation does NOT prevent multiple
    workers from independently bypassing the same limit. For multi-process
    deployments, MUST use the Redis-backed implementation.
    """

    def check_and_record(
        self,
        workspace_id: str,
        tool_name: str,
        limit_per_min: int,
    ) -> bool:
        """Check if the call is within the rate limit. If yes, record it
        and return True. If no, return False (caller should reject)."""
        ...

    def remaining(
        self,
        workspace_id: str,
        tool_name: str,
        limit_per_min: int,
    ) -> int:
        """How many calls remain in the current window?"""
        ...


class InMemoryRateLimiter:
    """
    In-memory rate limiter. Single-process only.

    Uses a sliding window: keeps timestamps of calls in the last 60 seconds.
    When the bucket is full, new calls are rejected.

    LIMITATION: This is NOT distributed-safe. Two workers in separate
    processes each have their own bucket — they can collectively exceed
    the limit. For production, use RedisRateLimiter (Day 6).
    """

    def __init__(self):
        # (workspace_id, tool_name) -> list of timestamps
        self._buckets: dict[tuple[str, str], list[float]] = defaultdict(list)

    def check_and_record(
        self,
        workspace_id: str,
        tool_name: str,
        limit_per_min: int,
    ) -> bool:
        now = time.time()
        key = (workspace_id, tool_name)
        bucket = self._buckets[key]

        # Remove entries older than 60 seconds (sliding window)
        cutoff = now - 60.0
        self._buckets[key] = [t for t in bucket if t > cutoff]
        bucket = self._buckets[key]

        if len(bucket) >= limit_per_min:
            return False  # rate limited

        bucket.append(now)
        return True

    def remaining(
        self,
        workspace_id: str,
        tool_name: str,
        limit_per_min: int,
    ) -> int:
        now = time.time()
        key = (workspace_id, tool_name)
        bucket = self._buckets[key]
        cutoff = now - 60.0
        current = [t for t in bucket if t > cutoff]
        return max(0, limit_per_min - len(current))


__all__ = [
    "SecurityContext",
    "AuditLogger",
    "InMemoryAuditLogger",
    "RateLimiter",
    "InMemoryRateLimiter",
]
