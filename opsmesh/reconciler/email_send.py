"""
OPSMESH Email Send Reconciler — resolves SEND_UNKNOWN runs.

Architecture (Correction 3):
  When send_email times out, the supervisor transitions the run to
  SEND_UNKNOWN (NOT to FAILED). This reconciler resolves the ambiguity:

    1. Find runs in SEND_UNKNOWN state.
    2. Query the email provider's get_status() API.
    3. If confirmed_sent → transition to SENT (via CRM_UPDATING).
    4. If confirmed_failed → transition to FAILED.
    5. If unknown → retry (up to 5 attempts). After 5, escalate (NOT auto-FAIL).

  CRITICAL: NEVER auto-transition to FAILED due to threshold exhaustion.
  Only CONCLUSIVE evidence from the provider resolves the ambiguity.
  If the outcome remains unknown after 5 attempts, the run stays in
  SEND_UNKNOWN and is escalated for manual investigation.

USAGE:
  reconciler = EmailSendReconciler(
      store=state_store,
      email_provider=email_adapter,
  )
  resolved_count = await reconciler.run_once()
"""

from __future__ import annotations

import asyncio
from typing import Optional

from opsmesh.adapters.email.base import EmailProvider, EmailStatus
from opsmesh.models import RunRecord, utcnow
from opsmesh.state_machine import State, transition
from opsmesh.state_store import StateStore


# Maximum reconciliation attempts before escalation (Correction 3).
MAX_RECONCILIATION_ATTEMPTS = 5


class EmailSendReconciler:
    """
    Resolves SEND_UNKNOWN runs by querying the email provider.

    The reconciler does NOT send emails. It only QUERIES the provider's
    status API to determine if a previously-attempted send succeeded or failed.

    Only CONCLUSIVE evidence resolves the ambiguity:
        - confirmed_sent → SENT
        - confirmed_failed → FAILED
        - unknown → retry (up to 5, then escalate)
    """

    def __init__(
        self,
        store: StateStore,
        email_provider: EmailProvider,
    ):
        self.store = store
        self.email_provider = email_provider
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def run_once(self, workspace_id: Optional[str] = None) -> int:
        """Find SEND_UNKNOWN runs and reconcile them.

        Args:
            workspace_id: if provided, only reconcile runs in this workspace.
                If None, reconcile across all workspaces.

        Returns:
            The number of runs resolved (transitioned to SENT or FAILED).
        """
        resolved_count = 0

        # Find all runs in SEND_UNKNOWN state
        # (InMemoryStateStore doesn't have a cross-workspace query, so we
        # iterate all runs. Day 6's PostgresStateStore will use a WHERE clause.)
        if hasattr(self.store, '_runs'):
            for run_id, run in list(self.store._runs.items()):
                if workspace_id and run.workspace_id != workspace_id:
                    continue
                if run.state != State.SEND_UNKNOWN:
                    continue

                resolved = await self._reconcile_run(run)
                if resolved:
                    resolved_count += 1

        return resolved_count

    async def _reconcile_run(self, run: RunRecord) -> bool:
        """Reconcile a single SEND_UNKNOWN run.

        FIX 3: Uses run.email_message_id (the real provider message ID)
        instead of run.run_id. Uses run.reconciliation_attempts (a dedicated
        counter) instead of run.critic_attempts.

        Returns True if the run was resolved (transitioned to SENT or FAILED).
        Returns False if the outcome is still unknown.
        """
        # Use the real provider message_id (Fix 3)
        message_id = run.email_message_id
        if message_id is None:
            # No message_id — can't query the provider. Escalate.
            run.reconciliation_attempts += 1
            run.last_error = (
                f"Cannot reconcile: no email_message_id stored for run. "
                f"Manual investigation required."
            )
            await self.store.save_checkpoint(run)
            return False

        try:
            status = await self.email_provider.get_status(message_id)
        except Exception:
            # Provider query failed — increment counter, don't transition
            run.reconciliation_attempts += 1
            await self.store.save_checkpoint(run)
            return False

        if status.status == "confirmed_sent":
            # Conclusive success → transition to SENT
            try:
                run.state = transition(run.state, State.SENT)
                await self.store.save_checkpoint(run)
                return True
            except Exception:
                return False

        elif status.status == "confirmed_failed":
            # Conclusive failure → transition to FAILED
            try:
                run.state = transition(run.state, State.FAILED)
                run.last_error = f"Email send confirmed failed: {status.detail}"
                await self.store.save_checkpoint(run)
                return True
            except Exception:
                return False

        else:
            # unknown — increment the DEDICATED reconciliation counter (Fix 3)
            run.reconciliation_attempts += 1
            if run.reconciliation_attempts >= MAX_RECONCILIATION_ATTEMPTS:
                # Escalate: DON'T auto-FAIL. Leave in SEND_UNKNOWN.
                run.last_error = (
                    f"Email send status unknown after {MAX_RECONCILIATION_ATTEMPTS} "
                    f"reconciliation attempts. Manual investigation required."
                )
                await self.store.save_checkpoint(run)
            return False

    async def start_in_background(
        self, interval_seconds: int = 60
    ) -> asyncio.Task:
        """Start the reconciler as a background task (runs every 60s)."""
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


__all__ = ["EmailSendReconciler", "MAX_RECONCILIATION_ATTEMPTS"]
