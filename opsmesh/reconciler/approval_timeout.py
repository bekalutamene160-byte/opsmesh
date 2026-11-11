"""
OPSMESH Approval Timeout Reconciler — expires pending approvals after 24h.

Architecture (Engineering Contract §1):
  Pending approvals expire after 24 hours (configurable). This reconciler
  runs periodically (every 5 minutes), finds expired approvals, marks them
  EXPIRED, and transitions the run to APPROVAL_EXPIRED (terminal).

  CRITICAL: timeout never means approval. Silence never means yes.
  A timed-out approval transitions the run to a TERMINAL state (APPROVAL_EXPIRED),
  NOT to SENDING. The email is NOT sent.

USAGE:
  reconciler = ApprovalTimeoutReconciler(
      store=state_store,
      approval_store=approval_store,
  )
  expired_count = await reconciler.run_once()
  # Returns the number of approvals expired in this run.

  # Or run as a background task:
  task = await reconciler.start_in_background(interval_seconds=300)
  await reconciler.stop()  # on shutdown
"""

from __future__ import annotations

import asyncio
from typing import Optional

from opsmesh.approval_store import ApprovalStore
from opsmesh.models import ApprovalStatus, utcnow
from opsmesh.state_machine import State, transition
from opsmesh.state_store import StateStore


class ApprovalTimeoutReconciler:
    """
    Finds and expires pending approvals that have passed their 24h timeout.

    For each expired approval:
        1. Mark the approval record as EXPIRED.
        2. Load the run.
        3. Transition the run to APPROVAL_EXPIRED (terminal).
        4. Save the checkpoint.

    If the run is NOT in a paused state (AWAITING_EVIDENCE_REVIEW or
    AWAITING_APPROVAL), the approval is expired but the run is NOT
    transitioned (it may have already been resolved by the human).
    """

    def __init__(
        self,
        store: StateStore,
        approval_store: ApprovalStore,
    ):
        self.store = store
        self.approval_store = approval_store
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def run_once(self) -> int:
        """Find and expire all pending approvals that have timed out.

        Returns the count of approvals expired.

        FIX 4: Uses the public interface method `find_expired_pending()`
        instead of accessing the private `_approvals` dict. This works with
        any ApprovalStore implementation (InMemory or Postgres).
        """
        expired_count = 0

        # Use the public interface method (Fix 4)
        expired_approvals = self.approval_store.find_expired_pending()

        for record in expired_approvals:
            # Expire this approval
            record.status = ApprovalStatus.EXPIRED
            expired_count += 1

            # Transition the run (if it's still paused)
            run = await self.store.load_latest(
                record.run_id, record.workspace_id
            )
            if run is not None and run.is_paused():
                try:
                    run.state = transition(
                        run.state, State.APPROVAL_EXPIRED
                    )
                    run.pending_approval_id = None
                    await self.store.save_checkpoint(run)
                except Exception:
                    pass  # illegal transition — run may have already moved

        return expired_count

    async def start_in_background(
        self, interval_seconds: int = 300
    ) -> asyncio.Task:
        """Start the reconciler as a background task.

        Runs run_once() every `interval_seconds` (default 5 minutes).
        """
        self._running = True

        async def loop():
            while self._running:
                try:
                    await self.run_once()
                except Exception:
                    pass  # log and continue
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


__all__ = ["ApprovalTimeoutReconciler"]
