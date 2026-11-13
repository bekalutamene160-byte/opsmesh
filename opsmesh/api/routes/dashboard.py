"""
OPSMESH Dashboard Route — GET /api/v1/dashboard.

Returns a JSON summary of the system state for the dashboard:
  - Recent runs (last 20, with state + timing)
  - Pending approvals (Gate 1 + Gate 2)
  - DLQ items
  - The 8 minimum-viable metrics

Architecture (course Ch 17):
  The dashboard is for situational awareness — every item answers an
  actionable question. If it doesn't, remove it.

  Returns JSON (not HTML). A frontend (Day 7) can consume this JSON
  and render a dashboard. For Day 6, the JSON is the dashboard.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from opsmesh.models import RunRecord
from opsmesh.observability.metrics import compute_metrics
from opsmesh.state_machine import State


router = APIRouter(prefix="/api/v1", tags=["dashboard"])

# Module-level references — set by configure_dashboard_route()
_store = None
_queue = None
_workspace_id = "default"


def configure_dashboard_route(store, queue=None, workspace_id: str = "default") -> None:
    """Configure the dashboard route with the given store + queue."""
    global _store, _queue, _workspace_id
    _store = store
    _queue = queue
    _workspace_id = workspace_id


@router.get("/dashboard")
async def get_dashboard():
    """Return the dashboard data as JSON.

    Includes:
      - metrics: the 8 minimum-viable metrics
      - recent_runs: last 20 runs (sorted by updated_at desc)
      - pending_approvals: runs in AWAITING_APPROVAL or AWAITING_EVIDENCE_REVIEW
      - dlq_items: dead-lettered jobs (if queue is configured)
    """
    if _store is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Dashboard not configured — store is not set"},
        )

    # Collect all runs in the workspace
    all_runs: list[RunRecord] = []
    for state in State:
        runs = await _store.list_in_state(_workspace_id, state.value, limit=100)
        all_runs.extend(runs)

    # Sort by updated_at descending and take the top 20
    all_runs.sort(key=lambda r: r.updated_at, reverse=True)
    recent_runs = all_runs[:20]

    # Pending approvals
    pending = [
        r for r in all_runs
        if r.state in (State.AWAITING_APPROVAL, State.AWAITING_EVIDENCE_REVIEW)
    ]

    # DLQ items
    dlq_items = []
    if _queue is not None:
        try:
            dlq_items = await _queue.list_dlq(_workspace_id)
        except Exception:
            dlq_items = []

    # Metrics
    metrics = await compute_metrics(
        store=_store,
        workspace_id=_workspace_id,
        queue=_queue,
    )

    return {
        "metrics": metrics,
        "recent_runs": [
            {
                "run_id": r.run_id,
                "lead_id": r.lead_id,
                "state": r.state.value if hasattr(r.state, 'value') else str(r.state),
                "started_at": r.started_at.isoformat(),
                "updated_at": r.updated_at.isoformat(),
                "cost_usd": round(r.budget_used_cost_usd, 6),
                "pending_approval_id": r.pending_approval_id,
                "last_error": r.last_error,
            }
            for r in recent_runs
        ],
        "pending_approvals": [
            {
                "run_id": r.run_id,
                "lead_id": r.lead_id,
                "state": r.state.value if hasattr(r.state, 'value') else str(r.state),
                "pending_approval_id": r.pending_approval_id,
                "updated_at": r.updated_at.isoformat(),
                # Stage B hotfix: surface what the approval is FOR so the
                # operator can see it inline in the dashboard + approvals
                # list without clicking into each one.
                "gate": "Gate 2 — Email Send"
                    if r.state == State.AWAITING_APPROVAL
                    else "Gate 1 — Evidence Review",
                "draft_subject": r.draft.subject if r.draft else None,
                "draft_recipient": r.draft.recipient if r.draft else None,
            }
            for r in pending
        ],
        "dlq_items": [
            {
                "job_id": j.id if hasattr(j, 'id') else str(j),
                "run_id": j.run_id if hasattr(j, 'run_id') else "",
                "last_error": j.last_error if hasattr(j, 'last_error') else "",
                "attempts": j.attempts if hasattr(j, 'attempts') else 0,
            }
            for j in dlq_items
        ],
    }


__all__ = ["router", "configure_dashboard_route"]
