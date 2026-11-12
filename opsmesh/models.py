"""
OPSMESH Pydantic models — the data shapes.

RunRecord is the core schema: the in-memory representation of a run that
gets serialized to JSON and stored in the `runs.payload` DB column.

ARCHITECTURE CORRECTIONS APPLIED:
  - Correction 6: LeadDisposition enum (contacted only after SENT).
  - Correction 7: ContactPolicy model (suppression list, opt-out).
  - Correction 14: `version` field for optimistic concurrency (CAS).
  - Correction 4: `parent_run_id` for retry linking.
  - Correction 2: CRMOutboxStatus + CRMOutboxOp models.
  - Correction 19: IdempotencyRecord as a typed Pydantic model.
  - Correction 9: LeadSource enum (no "scraped" allowed).

DESIGN PRINCIPLES:
  - Pydantic v2 (10x faster than v1; validates on every save/load).
  - Enums inherit from `str` so State.RESEARCHING → "researching" in JSON
    automatically. We do NOT use use_enum_values=True because that would
    store raw strings instead of State instances, breaking .value access.
  - Optional fields default to None (not empty containers) so
    `is not None` is the "has this step executed?" check.
  - All datetimes are UTC, timezone-aware (via utcnow()).
  - Numeric fields have constraints (ge=0, ge=1) to prevent invalid values.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from opsmesh.state_machine import State


def utcnow() -> datetime:
    """UTC now, timezone-aware. Never use datetime.utcnow() (naive)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums for lead lifecycle and CRM outbox (Corrections 2, 6)
# ---------------------------------------------------------------------------


class LeadSource(str, Enum):
    """How a lead entered the system. 'scraped' is deliberately absent."""
    MANUAL = "manual"
    WEBHOOK = "webhook"
    SIMULATED = "simulated"


class LeadDisposition(str, Enum):
    """
    The internal OPSMESH lead's current disposition.

    Rule: CONTACTED is set ONLY when the email send is definitively
    confirmed (run reached SENT). Never before.
    """
    NEW = "new"
    RESEARCHING = "researching"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"
    REJECTED = "rejected"
    CONTACTED = "contacted"
    STOPPED = "stopped"
    NEEDS_REVIEW = "needs_review"
    APPROVAL_EXPIRED = "approval_expired"
    FAILED = "failed"


class CRMOutboxStatus(str, Enum):
    """Independent lifecycle of a CRM outbox operation. (Correction 2.)"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class IdempotencyStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Sub-models: specialist outputs
# ---------------------------------------------------------------------------


class EvidenceItem(BaseModel):
    """A single piece of evidence collected by the Research specialist.

    Every material claim in a draft must cite an EvidenceItem.id.
    The Critic verifies this; the GroundingChecker guardrail re-verifies."""

    model_config = ConfigDict()

    id: str = Field(default_factory=lambda: f"ev_{uuid4().hex[:8]}")
    source: Literal["web", "lead_submitted", "crm", "manual"]
    source_url: Optional[str] = None
    content: str = Field(min_length=1)
    confidence: Literal["high", "medium", "low", "none"] = "medium"
    is_material: bool = False
    retrieved_at: datetime = Field(default_factory=utcnow)


class ResearchResult(BaseModel):
    """Output of the Research specialist.

    sufficient_for_qualification=False → supervisor pauses at Gate 1."""

    model_config = ConfigDict()

    evidence: list[EvidenceItem] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    sufficient_for_qualification: bool


class QualificationResult(BaseModel):
    """Output of the Qualify specialist.

    DISQUALIFIED → supervisor short-circuits to FAILED.
    NEEDS_REVIEW → supervisor pauses at Gate 1 (if research agrees
    evidence is insufficient)."""

    model_config = ConfigDict()

    status: Literal["QUALIFIED", "NEEDS_REVIEW", "DISQUALIFIED"]
    score: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)
    matched_criteria: list[str] = Field(default_factory=list)
    unmatched_criteria: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    """A material factual claim in a draft, with citation to evidence."""

    text: str = Field(min_length=1)
    evidence_id: str  # FK to EvidenceItem.id — validated at supervisor level


class DraftResult(BaseModel):
    """Output of the Draft specialist."""

    subject: str = Field(max_length=200)
    body: str = Field(min_length=1)
    recipient: str = Field(min_length=3)
    claims: list[Claim] = Field(default_factory=list)


class CriticIssue(BaseModel):
    """A specific issue found by the Critic."""

    kind: Literal[
        "ungrounded_claim",
        "length_violation",
        "tone_violation",
        "recipient_mismatch",
        "injection_suspected",
    ]
    detail: str
    claim_text: Optional[str] = None


class CriticResult(BaseModel):
    """Output of the Critic specialist.

    APPROVED → supervisor transitions to AWAITING_APPROVAL (Gate 2).
    REJECTED → supervisor routes back to DRAFTING with feedback.
    Any other verdict → supervisor fails closed (Correction 17)."""

    model_config = ConfigDict()

    verdict: Literal["APPROVED", "REJECTED"]
    feedback: str = ""
    issues: list[CriticIssue] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# RunRecord — THE CORE STATE SCHEMA
# ---------------------------------------------------------------------------


class RunRecord(BaseModel):
    """
    The complete state of a single workflow run. Persisted as JSONB.

    THIS IS THE RECOVERY CONTRACT:
        When a run crashes, SELECT the latest row, read the state, resume.

    Fields:
        version          — monotonic counter for optimistic concurrency (CAS).
                           Incremented on every save_checkpoint. The StateStore
                           uses this for compare-and-swap writes.
        step_id          — human-readable step counter for audit/timeline.
        parent_run_id    — if this run is a retry of a failed run, links to
                           the original. None for first attempts. (Correction 4.)
        draft_attempts   — how many times the Draft specialist has been called
                           in this run. Max 2 before FAILED.
    """

    # NOTE: we do NOT use arbitrary_types_allowed=True. All fields are
    # standard Pydantic types (str, int, float, datetime) or BaseModel
    # subclasses. State is a str-Enum which Pydantic v2 handles natively.
    # Using arbitrary_types_allowed=True causes Pydantic to skip enum
    # coercion, leaving state as a raw string — which breaks .value access.
    model_config = ConfigDict()

    # --- Identity ---
    run_id: str = Field(default_factory=lambda: f"run_{uuid4().hex[:12]}")
    workspace_id: str
    lead_id: str
    parent_run_id: Optional[str] = None  # for retry linking

    # --- State ---
    state: State = State.NEW
    step_id: int = Field(default=0, ge=0)
    version: int = Field(default=0, ge=0)  # CAS version (Correction 14)

    # --- Specialist results (None until the step executes) ---
    research: Optional[ResearchResult] = None
    qualification: Optional[QualificationResult] = None
    draft: Optional[DraftResult] = None
    critic_feedback: Optional[str] = None

    # --- Approval tracking ---
    pending_approval_id: Optional[str] = None

    # --- Idempotency tracking (forensic) ---
    idempotency_keys: list[str] = Field(default_factory=list)

    # --- Budget tracking (persisted for dashboard) ---
    budget_used_tokens: int = Field(default=0, ge=0)
    budget_used_cost_usd: float = Field(default=0.0, ge=0.0)
    budget_used_tool_calls: int = Field(default=0, ge=0)
    budget_started_at: datetime = Field(default_factory=utcnow)
    budget_max_tokens: int = Field(default=8000, ge=1)
    budget_max_cost_usd: float = Field(default=0.05, ge=0.0)
    budget_max_seconds: float = Field(default=60.0, gt=0.0)
    budget_max_tool_calls: int = Field(default=12, ge=1)

    # --- Timestamps (all UTC, timezone-aware) ---
    started_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    terminal_at: Optional[datetime] = None

    # --- Retry counters ---
    draft_attempts: int = Field(default=0, ge=0)
    critic_attempts: int = Field(default=0, ge=0)

    # --- Email send reconciliation (Fix 3: proper fields, not placeholders) ---
    # The provider's message_id from the email send attempt. Used by the
    # EmailSendReconciler to query get_status(). None if no send was attempted.
    email_message_id: Optional[str] = None
    # Dedicated counter for SEND_UNKNOWN reconciliation attempts. Separate
    # from critic_attempts (which counts Critic specialist calls).
    # After MAX_RECONCILIATION_ATTEMPTS (5), the run is escalated — NOT auto-FAILED.
    reconciliation_attempts: int = Field(default=0, ge=0)

    # --- Last error (sanitized; NEVER contains secrets) ---
    last_error: Optional[str] = None

    # --- Validation ---
    @field_validator("state", mode="before")
    @classmethod
    def coerce_state(cls, v):
        """Ensure state is always a State instance, even if loaded from JSON."""
        if isinstance(v, str):
            return State(v)
        return v

    # --- Helpers ---
    def is_terminal(self) -> bool:
        return self.state in (
            State.SENT, State.REJECTED, State.FAILED,
            State.STOPPED_BY_USER, State.APPROVAL_EXPIRED,
        )

    def is_paused(self) -> bool:
        return self.state in (
            State.AWAITING_EVIDENCE_REVIEW,
            State.AWAITING_APPROVAL,
            State.SEND_UNKNOWN,
        )

    def touch(self) -> None:
        """Update updated_at to now. Call before every save_checkpoint."""
        self.updated_at = utcnow()

    def increment_version(self) -> int:
        """Atomically increment the CAS version. Returns the new value."""
        self.version += 1
        return self.version


# ---------------------------------------------------------------------------
# Approval models (Day 5 will implement the full gate; Day 1 needs the shape)
# ---------------------------------------------------------------------------


class ApprovalKind(str, Enum):
    EVIDENCE_REVIEW = "evidence_review"  # Gate 1
    EMAIL_SEND = "email_send"           # Gate 2


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REQUEST_CHANGES = "request_changes"
    SUPERSEDED = "superseded"   # a newer approval replaced this one
    EXPIRED = "expired"
    CONSUMED = "consumed"       # Day 5: approval was used (single-use)


class ApprovalRecord(BaseModel):
    """A first-class approval record. Persisted in the `approvals` table.

    The ToolGateway checks THIS record (not the run state) before allowing
    send_email to execute. (Correction 5: Gate 1 never authorizes sending.)"""

    model_config = ConfigDict()

    id: str = Field(default_factory=lambda: f"ap_{uuid4().hex[:12]}")
    workspace_id: str
    run_id: str
    kind: ApprovalKind
    status: ApprovalStatus = ApprovalStatus.PENDING
    action: str
    payload: dict[str, Any]
    payload_hash: str
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    # Day 5: the human's decision and optional feedback
    decision: Optional[str] = None  # "approve" | "reject" | "request_changes" | "proceed_with_limitations" | "request_more_info" | "stop"
    decision_feedback: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime


# ---------------------------------------------------------------------------
# Job — the queue's work item (Day 2 implements JobQueue)
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class Job(BaseModel):
    """A unit of work claimed from the queue by a worker."""

    model_config = ConfigDict()

    id: str = Field(default_factory=lambda: f"job_{uuid4().hex[:12]}")
    workspace_id: str
    run_id: str
    status: JobStatus = JobStatus.QUEUED
    worker_id: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=4, ge=1)
    enqueued_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    last_error: Optional[str] = None


# ---------------------------------------------------------------------------
# IdempotencyRecord — typed (Correction 19)
# ---------------------------------------------------------------------------


class IdempotencyRecord(BaseModel):
    """
    A typed idempotency record. Prevents duplicate side effects on retry.

    The key is sha256(workspace_id + run_id + step_id + tool_name + args).
    Status lifecycle: pending → done | failed.
    """

    model_config = ConfigDict()

    key: str
    workspace_id: str
    run_id: str
    step_id: int = Field(ge=0)
    tool_name: str
    status: IdempotencyStatus = IdempotencyStatus.PENDING
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Lead + ContactPolicy (Corrections 6, 7)
# ---------------------------------------------------------------------------


class Lead(BaseModel):
    """
    The internal OPSMESH lead record. This is the source of truth for
    lead data. The external CRM is a downstream sync target.

    Every lead is durably recorded from intake onward, regardless of
    workflow outcome.
    """

    model_config = ConfigDict()

    id: str = Field(default_factory=lambda: f"lead_{uuid4().hex[:12]}")
    workspace_id: str
    crm_lead_id: Optional[str] = None  # set if external CRM is configured
    # lead_type: "company" (default, backward compatible) or "individual".
    # Individual leads may have no company -- they are people who filled
    # a contact form on a connected website. Research uses the website
    # if present, otherwise uses the notes/context the person provided.
    lead_type: str = Field(default="company", max_length=20)
    company: str = Field(default="", max_length=200)
    contact_email: str = Field(min_length=3)
    contact_name: Optional[str] = None
    industry: Optional[str] = None
    employees: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    website: Optional[str] = None
    # Free-form context the person filled (e.g., what they want to order,
    # their situation, questions). Used by research + draft when there is
    # no company website to analyze.
    notes: Optional[str] = None
    # Lead score 0-100, computed by the QualifySpecialist based on ICP fit,
    # research signals, and engagement. None until qualification runs.
    score: Optional[int] = Field(default=None, ge=0, le=100)

    source: LeadSource
    source_submission_id: Optional[str] = None  # for webhook dedup

    disposition: LeadDisposition = LeadDisposition.NEW
    disposition_reason: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ContactPolicy(BaseModel):
    """
    Workspace contact policy. Enforced pre-send; human approval does
    NOT override these checks. (Correction 7.)

    Day 4 (ToolGateway) will check suppression_list before send_email.
    """

    model_config = ConfigDict()

    workspace_id: str
    allowed_sources: list[LeadSource] = Field(
        default_factory=lambda: [LeadSource.MANUAL, LeadSource.WEBHOOK]
    )
    suppression_list: list[str] = Field(default_factory=list)  # email addresses
    sender_identity: str  # e.g., "Bekalu <bekalu@opsmesh.dev>"
    requires_explicit_consent: bool = True


# ---------------------------------------------------------------------------
# CRMOutboxOp (Correction 2)
# ---------------------------------------------------------------------------


class CRMOutboxOperation(str, Enum):
    CREATE_LEAD = "create_lead"
    UPDATE_LEAD_STATUS = "update_lead_status"


class CRMOutboxOp(BaseModel):
    """
    A pending CRM synchronization operation.

    Written to the `crm_outbox` table in the SAME transaction as the
    run state transition to CRM_UPDATING. The run can then transition
    to SENT; the outbox op is drained independently by the reconciler.

    SENT does NOT mean this op is DONE. The outbox has its own lifecycle.
    """

    model_config = ConfigDict()

    id: str = Field(default_factory=lambda: f"op_{uuid4().hex[:12]}")
    workspace_id: str
    run_id: str
    lead_id: str
    operation: CRMOutboxOperation
    args: dict[str, Any]
    status: CRMOutboxStatus = CRMOutboxStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=5, ge=1)
    next_attempt_at: datetime = Field(default_factory=utcnow)
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    completed_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# SentEmail — durable log of every email the agent sent (Phase 8)
# ---------------------------------------------------------------------------


class SentEmail(BaseModel):
    """
    A durable record of an email the agent sent to a lead.

    This is the source of truth for the Activity / Outbox tab. It captures
    the full content (subject + body), the recipient, the provider's
    message_id, and the send status -- so the operator can see exactly
    what was sent, to whom, and when, without parsing run payloads.
    """

    model_config = ConfigDict()

    id: str = Field(default_factory=lambda: f"se_{uuid4().hex[:12]}")
    workspace_id: str
    lead_id: str
    run_id: str
    recipient: str
    from_address: str
    subject: str
    body: str
    # "sent" (provider accepted), "skipped" (kill switch or no provider),
    # "failed" (provider returned an error), "unknown" (timed out).
    status: str = "sent"
    provider_message_id: Optional[str] = None
    lead_type: str = "company"
    sent_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# LeadHistoryEvent — the agent's memory of every interaction with a lead
# ---------------------------------------------------------------------------


class LeadHistoryEvent(BaseModel):
    """
    A single event in the agent's interaction history with a lead.

    Every pipeline step (research, qualify, draft, critic, approve, send,
    crm_update) logs one event here. This gives the agent a durable memory
    of what it did with each lead, visible in the Activity tab and on the
    lead detail page. It also enables "remember the user's history" --
    when a lead re-enters the pipeline, past events are available as context.
    """

    model_config = ConfigDict()

    id: str = Field(default_factory=lambda: f"he_{uuid4().hex[:12]}")
    workspace_id: str
    lead_id: str
    run_id: Optional[str] = None
    event_type: str  # "research" | "qualify" | "draft" | "critic" | "approve" | "send" | "crm_update" | "reject" | "create"
    summary: str = ""  # short human-readable description
    detail: Optional[str] = None  # longer context (research summary, draft preview, etc.)
    created_at: datetime = Field(default_factory=utcnow)


__all__ = [
    "utcnow",
    # Enums
    "LeadSource", "LeadDisposition", "CRMOutboxStatus", "IdempotencyStatus",
    "ApprovalKind", "ApprovalStatus", "JobStatus", "CRMOutboxOperation",
    # Sub-models
    "EvidenceItem", "ResearchResult", "QualificationResult",
    "Claim", "DraftResult", "CriticIssue", "CriticResult",
    # Core
    "RunRecord", "ApprovalRecord", "Job", "IdempotencyRecord",
    "Lead", "ContactPolicy", "CRMOutboxOp",
    # Phase 8
    "SentEmail", "LeadHistoryEvent",
]
