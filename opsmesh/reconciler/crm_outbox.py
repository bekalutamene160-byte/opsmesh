"""
OPSMESH CRM Outbox Drainer — independent recovery of CRM sync operations.

Architecture (Correction 2 + ADR-009):
  When a run reaches SENT (email confirmed), a CRM outbox operation is
  created to update the external CRM (e.g., mark the lead as "contacted").

  The outbox op is INDEPENDENT of the email send:
    - If the CRM is unavailable, the email was STILL sent. The outbox
      retains the pending op; the drainer retries independently.
    - CRM failure NEVER triggers email resend (Correction 2).
    - The run stays in SENT (terminal) — the outbox has its own lifecycle.

  The drainer claims pending ops and calls the CRM adapter:
    - On success → mark DONE.
    - On transient failure (5xx, timeout) → retry with backoff (max 5 attempts).
    - On permanent failure (4xx) → mark FAILED, alert operator.
    - After max_attempts → DEAD_LETTER.

USAGE:
  drainer = CRMOutboxDrainer(
      crm_provider=crm_adapter,
      outbox_store=outbox_store,
  )
  drained_count = await drainer.run_once()
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Optional

from opsmesh.adapters.crm.base import CRMProvider, ToolError as CRMToolError
from opsmesh.models import CRMOutboxOp, CRMOutboxStatus, utcnow
from opsmesh.state_store import StateStore


# Backoff schedule for outbox retries (seconds): 30, 60, 120, 240, 480
BACKOFF_SCHEDULE = [30, 60, 120, 240, 480]


class CRMOutboxStore:
    """
    In-memory CRM outbox store. Day 6 replaces with Postgres `crm_outbox` table.

    The store holds pending CRM operations. The drainer claims and drains them.
    """

    def __init__(self):
        self._ops: dict[str, CRMOutboxOp] = {}

    def enqueue(self, op: CRMOutboxOp) -> None:
        """Add a pending outbox operation."""
        self._ops[op.id] = op

    def get(self, op_id: str) -> Optional[CRMOutboxOp]:
        return self._ops.get(op_id)

    def claim_pending(self, limit: int = 10) -> list[CRMOutboxOp]:
        """Claim up to `limit` pending ops whose next_attempt_at has passed.

        Marks them IN_PROGRESS so other workers don't claim them.
        """
        now = utcnow()
        claimed = []
        for op in self._ops.values():
            if op.status == CRMOutboxStatus.PENDING and op.next_attempt_at <= now:
                op.status = CRMOutboxStatus.IN_PROGRESS
                op.attempts += 1
                claimed.append(op)
                if len(claimed) >= limit:
                    break
        return claimed

    def mark_done(self, op_id: str) -> None:
        op = self._ops.get(op_id)
        if op is not None:
            op.status = CRMOutboxStatus.DONE
            op.completed_at = utcnow()

    def mark_failed(self, op_id: str, error: str) -> None:
        op = self._ops.get(op_id)
        if op is not None:
            op.status = CRMOutboxStatus.FAILED
            op.last_error = error
            op.completed_at = utcnow()

    def mark_dead_letter(self, op_id: str, error: str) -> None:
        op = self._ops.get(op_id)
        if op is not None:
            op.status = CRMOutboxStatus.DEAD_LETTER
            op.last_error = error
            op.completed_at = utcnow()

    def requeue(self, op_id: str, error: str) -> None:
        """Put a transiently-failed op back to PENDING with backoff."""
        op = self._ops.get(op_id)
        if op is not None:
            op.status = CRMOutboxStatus.PENDING
            op.last_error = error
            # Calculate backoff delay based on attempt count
            idx = min(op.attempts - 1, len(BACKOFF_SCHEDULE) - 1)
            delay = BACKOFF_SCHEDULE[idx]
            op.next_attempt_at = utcnow() + timedelta(seconds=delay)

    def list_pending(self) -> list[CRMOutboxOp]:
        return [op for op in self._ops.values() if op.status == CRMOutboxStatus.PENDING]

    def list_done(self) -> list[CRMOutboxOp]:
        return [op for op in self._ops.values() if op.status == CRMOutboxStatus.DONE]

    def list_dead_letter(self) -> list[CRMOutboxOp]:
        return [op for op in self._ops.values() if op.status == CRMOutboxStatus.DEAD_LETTER]


class CRMOutboxDrainer:
    """
    Drains the CRM outbox — claims pending ops and calls the CRM adapter.

    INDEPENDENT RECOVERY (Correction 2):
      - The email was already sent (run is in SENT).
      - CRM sync failure does NOT trigger email resend.
      - The drainer retries with backoff (max 5 attempts).
      - After max_attempts → DEAD_LETTER (operator alerted).
    """

    def __init__(
        self,
        crm_provider: CRMProvider,
        outbox_store: CRMOutboxStore,
    ):
        self.crm = crm_provider
        self.outbox = outbox_store
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def run_once(self) -> int:
        """Drain pending outbox ops. Returns the count of ops completed."""
        completed_count = 0

        pending = self.outbox.claim_pending(limit=10)
        for op in pending:
            success = await self._drain_one(op)
            if success:
                completed_count += 1

        return completed_count

    async def _drain_one(self, op: CRMOutboxOp) -> bool:
        """Drain a single outbox operation. Returns True if completed (DONE)."""
        try:
            if op.operation.value == "update_lead_status":
                await self.crm.update_lead_status(
                    workspace_id=op.workspace_id,
                    lead_id=op.args.get("lead_id", op.lead_id),
                    status=op.args.get("status", "contacted"),
                )
            elif op.operation.value == "create_lead":
                from opsmesh.adapters.crm.base import CRMLeadCreate
                from opsmesh.models import LeadSource
                create_args = CRMLeadCreate(
                    workspace_id=op.workspace_id,
                    company=op.args.get("company", ""),
                    contact_email=op.args.get("contact_email", ""),
                    contact_name=op.args.get("contact_name"),
                    industry=op.args.get("industry"),
                    employees=op.args.get("employees"),
                    source=LeadSource(op.args.get("source", "manual")),
                )
                await self.crm.create_lead(create_args)

            # Success → mark DONE
            self.outbox.mark_done(op.id)
            return True

        except CRMToolError as e:
            # Permanent failure (4xx) → mark FAILED, alert operator
            if e.code in ("invalid_status", "not_found", "forbidden"):
                self.outbox.mark_failed(op.id, f"{e.code}: {e.message}")
                return False
            # Transient failure → requeue with backoff
            if op.attempts >= op.max_attempts:
                self.outbox.mark_dead_letter(op.id, f"{e.code}: {e.message}")
            else:
                self.outbox.requeue(op.id, f"{e.code}: {e.message}")
            return False

        except Exception as e:
            # Transient failure (timeout, 5xx) → requeue with backoff
            safe_error = f"{type(e).__name__}: {str(e)[:200]}"
            if op.attempts >= op.max_attempts:
                self.outbox.mark_dead_letter(op.id, safe_error)
            else:
                self.outbox.requeue(op.id, safe_error)
            return False

    async def start_in_background(
        self, interval_seconds: int = 30
    ) -> asyncio.Task:
        """Start the drainer as a background task (runs every 30s)."""
        self._running = True

        async def loop():
            while self._running:
                try:
                    await self.run_once()
                except Exception:
                    pass
                await asyncio.sleep(interval_seconds)

        self._task = asyncio.create_task(loop())
        return self._task

    async def stop(self) -> None:
        """Stop the background task."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None


__all__ = [
    "CRMOutboxStore",
    "CRMOutboxDrainer",
    "BACKOFF_SCHEDULE",
]
