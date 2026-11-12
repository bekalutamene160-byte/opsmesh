"""
OPSMESH LeadIntakeService — the single entry point for all 3 intake paths.

Architecture (FR-5): All three intake paths invoke the same
LeadIntakeService.create_lead method:
  1. Manual UI entry (POST /api/v1/leads)
  2. Authenticated webhook (POST /api/v1/events/new_lead)
  3. Simulated event (POST /api/v1/demo/simulate)

Why a single service?
  - Consistency: dedup, lead creation, and job enqueue happen identically.
  - Testability: one service to test, not three.
  - Auditability: every lead goes through the same code path.

Webhook dedup (FR-4, SEC-3):
  Each webhook event includes a submission_id. The leads table has a UNIQUE
  constraint on (workspace_id, source_submission_id). If the same
  submission_id arrives within 5 minutes, the second call returns
  {status: "duplicate"} without creating a new lead or job.

Phase 7 architectural fix:
  Before Phase 7, LeadIntakeService kept leads in an in-memory dict
  (`_leads`). This was fine for tests but a production bug — leads
  disappeared on restart and weren't visible across workers.

  Now LeadIntakeService delegates lead storage to a LeadStore (which can
  be InMemoryLeadStore for tests/dev, or DatabaseLeadStore for production).
  The dedup index stays in-memory (it's a 5-minute window, not a
  persistence concern).

Contact policy (Correction 7):
  - Lead source is tracked (manual, webhook, simulated — never "scraped").
  - Suppression list is checked before enqueue.
  - Human approval does NOT override suppression (enforced at ToolGateway
    Day 4, but LeadIntakeService also checks at intake to fail fast).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from opsmesh.adapters.db.lead_store import (
    DatabaseLeadStore,
    InMemoryLeadStore,
    LeadStore,
    create_lead_store,
)
from opsmesh.models import Lead, LeadDisposition, LeadSource, RunRecord, utcnow
from opsmesh.state_machine import State
from opsmesh.state_store import StateStore
from opsmesh.job_queue import JobQueueProtocol


# Dedup window: 5 minutes (Engineering Contract).
DEDUP_WINDOW_SECONDS = 300


class LeadIntakeError(Exception):
    """Base class for LeadIntakeService errors."""


class DuplicateSubmission(LeadIntakeError):
    """The same submission_id was seen within the dedup window."""

    def __init__(self, submission_id: str, lead_id: str):
        self.submission_id = submission_id
        self.lead_id = lead_id
        super().__init__(
            f"Duplicate submission_id={submission_id} within {DEDUP_WINDOW_SECONDS}s. "
            f"Existing lead_id={lead_id}."
        )


class SuppressedEmail(LeadIntakeError):
    """The contact email is on the workspace suppression list."""

    def __init__(self, email: str):
        self.email = email
        super().__init__(
            f"Email {email} is on the suppression list. Lead rejected."
        )


class LeadIntakeService:
    """
    Single entry point for all lead creation.

    Usage:
        service = LeadIntakeService(store=..., queue=...)
        result = await service.create_lead(
            workspace_id="ws_1",
            company="Acme Corp",
            contact_email="founder@acme.com",
            source=LeadSource.MANUAL,
        )
        # result.lead_id, result.run_id, result.job_id

    The service:
      1. Checks dedup (if submission_id given).
      2. Checks suppression list (if policy given).
      3. Creates a Lead record (disposition=NEW) — persisted via LeadStore.
      4. Creates a RunRecord (state=NEW).
      5. Enqueues a Job in the queue.
      6. Returns lead_id, run_id, job_id.
    """

    def __init__(
        self,
        store: StateStore,
        queue: JobQueueProtocol,
        suppression_list: Optional[set[str]] = None,
        lead_store: Optional[LeadStore] = None,
        history_store=None,
    ) -> None:
        self.store = store
        self.queue = queue
        # Lead persistence: LeadStore (DB-backed in production)
        # If not provided, auto-create one based on the configured backend.
        # For tests that pass an explicit lead_store (e.g., InMemoryLeadStore),
        # use that. For backward compat, if no lead_store and no DB configured,
        # use InMemoryLeadStore.
        self.lead_store: LeadStore = lead_store or create_lead_store()
        # Lead history store (the agent's memory). Optional -- if None,
        # history events are not logged (backward compat for tests).
        self.history_store = history_store
        # In-memory dedup index: (workspace_id, submission_id) -> (lead_id, created_at)
        # Stays in-memory because it's a 5-minute window, not persistence.
        self._dedup_index: dict[tuple[str, str], tuple[str, float]] = {}
        # Suppression list (workspace_id -> set of emails)
        self._suppression: dict[str, set[str]] = {}
        # Global suppression (applies to all workspaces — constructor convenience)
        if suppression_list:
            self._global_suppression = {e.lower() for e in suppression_list}
        else:
            self._global_suppression = set()

    def set_suppression_list(
        self, workspace_id: str, emails: set[str]
    ) -> None:
        """Set the suppression list for a workspace."""
        self._suppression[workspace_id] = set(emails)

    def _is_suppressed(self, workspace_id: str, email: str) -> bool:
        """Check if an email is suppressed (globally or in this workspace)."""
        if email.lower() in self._global_suppression:
            return True
        suppressed = self._suppression.get(workspace_id, set())
        return email.lower() in {e.lower() for e in suppressed}

    async def _check_dedup(
        self, workspace_id: str, submission_id: Optional[str]
    ) -> Optional[str]:
        """Returns the existing lead_id if this submission was seen recently,
        else None.

        Checks BOTH the in-memory dedup index (5-min window) AND the
        database (for submissions that arrived before a restart, but are
        still within the dedup window after restart).
        """
        if submission_id is None:
            return None

        # 1. Check in-memory index (fast path)
        key = (workspace_id, submission_id)
        entry = self._dedup_index.get(key)
        if entry is not None:
            lead_id, created_at_ts = entry
            age = (utcnow().timestamp() - created_at_ts)
            if age > DEDUP_WINDOW_SECONDS:
                # Expired — clear and allow
                del self._dedup_index[key]
            else:
                return lead_id

        # 2. Check the database (covers restarts)
        # Only DatabaseLeadStore has find_by_submission that hits the DB;
        # InMemoryLeadStore also has it (just checks the in-memory dict).
        existing = await self.lead_store.find_by_submission(workspace_id, submission_id)
        if existing is not None:
            # Found in DB — record in the in-memory index for next time
            self._dedup_index[key] = (existing.id, existing.created_at.timestamp())
            return existing.id

        return None

    def _record_submission(
        self, workspace_id: str, submission_id: str, lead_id: str
    ) -> None:
        """Record that we've seen this submission_id (for dedup)."""
        self._dedup_index[(workspace_id, submission_id)] = (
            lead_id, utcnow().timestamp()
        )

    async def create_lead(
        self,
        *,
        workspace_id: str,
        company: str = "",
        contact_email: str,
        contact_name: Optional[str] = None,
        industry: Optional[str] = None,
        employees: Optional[int] = None,
        website: Optional[str] = None,
        source: LeadSource,
        submission_id: Optional[str] = None,
        lead_type: str = "company",
        notes: Optional[str] = None,
    ) -> "LeadIntakeResult":
        """
        Create a lead, initialize a run, and enqueue a job.

        Supports both company leads (lead_type="company", company required)
        and individual leads (lead_type="individual", company may be empty).
        For individuals, the `notes` field carries the context they filled
        on a connected contact form (e.g., what they want to order).

        Steps:
          1. Check dedup (if submission_id given). If duplicate, raise
             DuplicateSubmission.
          2. Check suppression list. If suppressed, raise SuppressedEmail.
          3. Create a Lead (disposition=NEW), persist via LeadStore.
          4. Create a RunRecord (state=NEW), save checkpoint.
          5. Enqueue a Job.
          6. Record submission_id for future dedup.
          7. Log a "create" history event (the agent's memory).
          8. Return LeadIntakeResult with lead_id, run_id, job_id.
        """
        # 1. Dedup check (in-memory + DB)
        existing_lead_id = await self._check_dedup(workspace_id, submission_id)
        if existing_lead_id is not None:
            raise DuplicateSubmission(submission_id or "", existing_lead_id)

        # 2. Suppression check (Correction 7)
        if self._is_suppressed(workspace_id, contact_email):
            raise SuppressedEmail(contact_email)

        # 3. Create the Lead (persist via LeadStore)
        lead = Lead(
            workspace_id=workspace_id,
            lead_type=lead_type,
            company=company,
            contact_email=contact_email,
            contact_name=contact_name,
            industry=industry,
            employees=employees,
            website=website,
            notes=notes,
            source=source,
            source_submission_id=submission_id,
            disposition=LeadDisposition.NEW,
        )
        await self.lead_store.save(lead)

        # 4. Create the RunRecord (state=NEW), save checkpoint
        run = RunRecord(
            workspace_id=workspace_id,
            lead_id=lead.id,
            state=State.NEW,
        )
        await self.store.save_checkpoint(run)

        # 5. Enqueue a job
        job_id = await self.queue.enqueue(workspace_id, run.run_id)

        # 6. Record submission for dedup
        if submission_id is not None:
            self._record_submission(workspace_id, submission_id, lead.id)

        # 7. Log a "create" history event (the agent's memory)
        if self.history_store is not None:
            from opsmesh.models import LeadHistoryEvent
            desc = (
                f"New {lead_type} lead: {company or contact_name or contact_email}"
            )
            await self.history_store.save(LeadHistoryEvent(
                workspace_id=workspace_id,
                lead_id=lead.id,
                run_id=run.run_id,
                event_type="create",
                summary=desc,
                detail=notes if notes else None,
            ))

        return LeadIntakeResult(
            lead_id=lead.id,
            run_id=run.run_id,
            job_id=job_id,
        )

    async def get_lead(
        self, workspace_id: str, lead_id: str
    ) -> Optional[Lead]:
        """Fetch a lead. Returns None if not found OR cross-workspace."""
        return await self.lead_store.get(workspace_id, lead_id)

    async def list_leads(
        self,
        workspace_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Lead], int]:
        """List leads for a workspace, sorted by created_at descending.

        Returns (leads_page, total_count). The total_count is the full
        count for the workspace (so the frontend can render pagination
        metadata); leads_page is the slice for the current page.

        Workspace isolation is enforced: only leads whose workspace_id
        matches are returned. Cross-workspace leads are invisible.
        """
        return await self.lead_store.list_leads(workspace_id, limit, offset)

    async def update_lead_disposition(
        self,
        workspace_id: str,
        lead_id: str,
        disposition: LeadDisposition,
        reason: Optional[str] = None,
    ) -> None:
        """Update a lead's disposition. Used by the supervisor when a run
        reaches a terminal state (Day 5 wires this up).

        Correction 6: CONTACTED is set ONLY when the run reaches SENT.
        The supervisor's _apply_result enforces this — it never sets
        CONTACTED before SENT."""
        await self.lead_store.update_disposition(workspace_id, lead_id, disposition, reason)


class LeadIntakeResult:
    """Result of LeadIntakeService.create_lead."""

    def __init__(self, lead_id: str, run_id: str, job_id: str):
        self.lead_id = lead_id
        self.run_id = run_id
        self.job_id = job_id


__all__ = [
    "LeadIntakeService",
    "LeadIntakeResult",
    "LeadIntakeError",
    "DuplicateSubmission",
    "SuppressedEmail",
    "DEDUP_WINDOW_SECONDS",
]
