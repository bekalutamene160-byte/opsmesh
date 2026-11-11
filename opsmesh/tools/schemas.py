"""
OPSMESH Tool Schemas — typed contracts for the 5 tools.

Architecture (Correction 9 — tool counting):
  - 5 MCP tools across 2 MCP servers (Day 4):
      CRM server:    lookup_lead, update_lead_status, create_lead
      Comms server:  send_email, send_notification
  - 1 inline tool: web_search (lives inside the Research specialist;
    no credentials to protect, so no MCP boundary needed)

Day 2 scope: define the Pydantic schemas (input + output) for each tool.
Day 3 will use these with the IdempotentToolExecutor.
Day 4 will host them in MCP servers + enforce policies via ToolGateway.

Design rules:
  - Every tool has typed Args and Result models (Pydantic v2).
  - Args use Field constraints (min_length, max_length, pattern, ge, le).
  - Status enums are explicit (sent, queued, failed — not "success boolean").
  - Email recipients use EmailStr for basic validation; the ToolGateway
    (Day 4) will enforce the workspace allowlist on top.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, EmailStr, Field, HttpUrl

from opsmesh.models import utcnow


# ---------------------------------------------------------------------------
# 1. lookup_lead (CRM READ — no approval required)
# ---------------------------------------------------------------------------


class LookupLeadArgs(BaseModel):
    """Args for the lookup_lead tool. CRM lead ID format: L_<digits>."""
    lead_id: str = Field(
        ...,
        pattern=r"^L_\d+$",
        description="CRM lead ID, format L_<digits>",
    )


class LookupLeadResult(BaseModel):
    """Result of lookup_lead. None-fields indicate missing CRM data."""

    lead_id: str
    company: str
    contact_email: str
    contact_name: Optional[str] = None
    industry: Optional[str] = None
    employees: Optional[int] = Field(default=None, ge=1)
    website: Optional[str] = None
    created_at: datetime
    last_modified_at: datetime


# ---------------------------------------------------------------------------
# 2. update_lead_status (CRM WRITE — requires Gate 2 approval upstream)
# ---------------------------------------------------------------------------


class UpdateLeadStatusArgs(BaseModel):
    """Args for update_lead_status. Status is constrained by Literal
    (defense in depth — the ToolGateway also enforces this on Day 4)."""

    lead_id: str = Field(..., pattern=r"^L_\d+$")
    status: Literal[
        "contacted",
        "qualified",
        "rejected",
        "disqualified",
        "needs_review",
    ]


class UpdateLeadStatusResult(BaseModel):
    """Result of update_lead_status."""

    lead_id: str
    previous_status: str
    new_status: str
    updated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# 3. create_lead (CRM WRITE — requires Gate 1 approval when unknown company)
# ---------------------------------------------------------------------------


class CreateLeadArgs(BaseModel):
    """Args for create_lead. Used when a webhook arrives for a company
    not yet in the CRM."""

    company: str = Field(..., min_length=1, max_length=200)
    contact_email: EmailStr
    contact_name: Optional[str] = Field(default=None, max_length=200)
    industry: Optional[str] = Field(default=None, max_length=100)
    employees: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    website: Optional[HttpUrl] = None
    source: Literal["manual", "webhook", "simulated"] = "manual"


class CreateLeadResult(BaseModel):
    """Result of create_lead. Returns the new CRM lead_id."""

    lead_id: str = Field(
        default_factory=lambda: f"L_{abs(hash(uuid4().hex)) % 1_000_000:06d}"
    )
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# 4. send_email (Comms NOTIFY — always requires Gate 2 approval)
# ---------------------------------------------------------------------------


class SendEmailArgs(BaseModel):
    """Args for send_email.

    approval_id is REQUIRED — the ToolGateway (Day 4) verifies the approval
    record exists, is approved, is non-expired, and the payload_hash matches
    before allowing the call to reach the provider.

    The `to` field uses EmailStr for basic format validation; the ToolGateway
    additionally enforces the workspace recipient allowlist (regex pattern)."""

    to: EmailStr = Field(..., description="Recipient email")
    from_: str = Field(..., description="Sender identity (workspace-configured)")
    subject: str = Field(..., max_length=200)
    body: str = Field(..., max_length=2000)
    approval_id: str = Field(..., pattern=r"^ap_[a-f0-9]{12}$")


class SendEmailResult(BaseModel):
    """Result of send_email.

    status is one of:
        - sent:     provider confirmed acceptance (definitive success)
        - queued:   provider accepted but hasn't sent yet (reconciler polls)
        - failed:   provider confirmed rejection (definitive failure)
    Ambiguous timeouts do NOT return SendEmailResult; they raise and the
    supervisor transitions the run to SEND_UNKNOWN (Correction 3)."""

    message_id: str = Field(
        default_factory=lambda: f"msg_{uuid4().hex[:16]}"
    )
    sent_at: datetime = Field(default_factory=utcnow)
    status: Literal["sent", "queued", "failed"]


# ---------------------------------------------------------------------------
# 5. send_notification (Comms NOTIFY — internal only, no Gate 2)
# ---------------------------------------------------------------------------


class SendNotificationArgs(BaseModel):
    """Args for send_notification. Internal notifications to the workspace
    owner (e.g., "approval requested"). Does NOT require Gate 2 approval."""

    to: Literal["workspace_owner"] = "workspace_owner"  # only v1 recipient
    subject: str = Field(..., max_length=200)
    body: str = Field(..., max_length=1000)
    kind: Literal[
        "approval_requested",
        "approval_decision",
        "run_failed",
        "dlq_alert",
    ]


class SendNotificationResult(BaseModel):
    """Result of send_notification."""

    notification_id: str = Field(
        default_factory=lambda: f"notif_{uuid4().hex[:12]}"
    )
    sent_at: datetime = Field(default_factory=utcnow)
    channel: Literal["email", "slack"] = "email"


# ---------------------------------------------------------------------------
# 6. web_search (inline research capability — NOT an MCP tool, Correction 9)
# ---------------------------------------------------------------------------


class WebSearchArgs(BaseModel):
    """Args for web_search. Inline in the Research specialist because it
    has no credentials to protect (uses a free search provider).

    SSRF protection (Day 4 ToolGateway): outbound HTTP is restricted to a
    configured domain allowlist; private IP ranges are blocked at the socket
    level."""

    query: str = Field(..., min_length=1, max_length=200)
    max_results: int = Field(default=5, ge=1, le=10)


class WebSearchHit(BaseModel):
    """A single search result."""

    url: HttpUrl
    title: str
    snippet: str
    retrieved_at: datetime = Field(default_factory=utcnow)


class WebSearchResult(BaseModel):
    """Result of web_search."""

    results: list[WebSearchHit] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool error model — every tool returns this on failure (not raw exceptions)
# ---------------------------------------------------------------------------


class ToolError(BaseModel):
    """Standard error response from any tool.

    `code` is a stable, machine-readable string. The supervisor / tool
    gateway can branch on code to decide retry vs. fail-closed."""

    code: Literal[
        "not_found",
        "forbidden",
        "invalid_status",
        "invalid_args",
        "rate_limited",
        "provider_timeout",
        "provider_error",
        "approval_required",
        "stale_approval",
        "email_disabled",
    ]
    message: str
    retry_after_seconds: Optional[int] = None  # for rate_limited


__all__ = [
    # 1. lookup_lead
    "LookupLeadArgs", "LookupLeadResult",
    # 2. update_lead_status
    "UpdateLeadStatusArgs", "UpdateLeadStatusResult",
    # 3. create_lead
    "CreateLeadArgs", "CreateLeadResult",
    # 4. send_email
    "SendEmailArgs", "SendEmailResult",
    # 5. send_notification
    "SendNotificationArgs", "SendNotificationResult",
    # 6. web_search (inline)
    "WebSearchArgs", "WebSearchHit", "WebSearchResult",
    # Errors
    "ToolError",
]
