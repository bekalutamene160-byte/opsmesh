"""
Email Provider Protocol — the interface for email adapters.

Architecture (ADR-008): mock-first. MockEmailProvider captures sent emails
to a list (for test assertions) and never makes a network call. Real
adapters (ResendEmailProvider, PostmarkEmailProvider) implement the same
Protocol.

The email adapter is called by:
  - The Comms MCP server's send_email tool (Day 4), gated by Gate 2 approval.
  - The reconciler's get_status() — to resolve SEND_UNKNOWN runs.

Ambiguous timeout behavior (Correction 3):
  If send() times out (no provider response), the adapter raises
  asyncio.TimeoutError. The supervisor catches this and transitions the
  run to SEND_UNKNOWN — NOT to FAILED. The reconciler later queries
  get_status() to resolve the ambiguity with conclusive evidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, EmailStr, Field

from opsmesh.models import utcnow


class EmailMessage(BaseModel):
    """A complete email message ready to send."""

    to: EmailStr
    from_: str
    subject: str = Field(max_length=200)
    body: str = Field(max_length=2000)
    approval_id: str = Field(pattern=r"^ap_[a-f0-9]{12}$")
    idempotency_key: Optional[str] = None  # provider-level dedup if supported


class EmailSendResult(BaseModel):
    """Result of a confirmed send."""

    message_id: str
    sent_at: datetime = Field(default_factory=utcnow)
    status: Literal["sent", "queued", "failed"]


class EmailStatus(BaseModel):
    """The status of a previously-sent email (for reconciliation).

    Used by the reconciler to resolve SEND_UNKNOWN runs:
      - confirmed_sent   → run transitions to SENT
      - confirmed_failed  → run transitions to FAILED
      - unknown           → reconciler retries (max 5 attempts; if still
                            unknown, escalates for manual investigation —
                            NEVER auto-resolves to FAILED, Correction 3)
    """

    message_id: str
    status: Literal["confirmed_sent", "confirmed_failed", "unknown"]
    detail: Optional[str] = None


@runtime_checkable
class EmailProvider(Protocol):
    """The email provider contract."""

    async def send(self, msg: EmailMessage) -> EmailSendResult:
        """
        Send an email. Returns EmailSendResult on success.

        Raises:
            asyncio.TimeoutError: ambiguous timeout (provider didn't respond).
                The supervisor transitions the run to SEND_UNKNOWN.
            ProviderError: confirmed failure (provider rejected the request).
                The supervisor transitions the run to FAILED.
        """
        ...

    async def get_status(self, message_id: str) -> EmailStatus:
        """Query the provider for the status of a previously-sent email.
        Used by the reconciler to resolve SEND_UNKNOWN runs."""
        ...


__all__ = ["EmailMessage", "EmailSendResult", "EmailStatus", "EmailProvider"]
