"""
OPSMESH Runs API — run inspection endpoints.

Endpoints:
  GET /api/v1/runs          — list runs (with pagination + state filter)
  GET /api/v1/runs/{id}     — get run detail (state, evidence, approvals, timeline)

Architecture:
  These endpoints read from the StateStore. They are authenticated and
  workspace-scoped — a user can only see runs in their own workspace.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from opsmesh.api.auth import get_current_user
from opsmesh.state_machine import State


router = APIRouter(prefix="/api/v1", tags=["runs"])

# Module-level store reference
_store = None


def configure_runs_route(store) -> None:
    """Configure the runs route with the given StateStore."""
    global _store
    _store = store


@router.get("/runs")
async def list_runs(
    state: Optional[str] = Query(None, description="Filter by state (e.g., 'awaiting_approval')"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List runs for the authenticated user's workspace.

    Supports filtering by state and pagination via limit/offset.
    """
    if _store is None:
        raise HTTPException(503, "Store not configured")

    workspace_id = user["workspace_id"]

    if state:
        # Filter by specific state
        runs = await _store.list_in_state(workspace_id, state, limit=limit + offset)
        # Apply offset
        runs = runs[offset:offset + limit]
    else:
        # List all states (iterate all 16 states)
        all_runs = []
        for s in State:
            state_runs = await _store.list_in_state(workspace_id, s.value, limit=100)
            all_runs.extend(state_runs)
        # Sort by updated_at descending
        all_runs.sort(key=lambda r: r.updated_at, reverse=True)
        runs = all_runs[offset:offset + limit]

    return {
        "runs": [
            {
                "run_id": r.run_id,
                "lead_id": r.lead_id,
                "state": r.state.value if hasattr(r.state, 'value') else str(r.state),
                "started_at": r.started_at.isoformat() if hasattr(r.started_at, 'isoformat') else str(r.started_at),
                "updated_at": r.updated_at.isoformat() if hasattr(r.updated_at, 'isoformat') else str(r.updated_at),
                "cost_usd": round(r.budget_used_cost_usd, 6),
                "pending_approval_id": r.pending_approval_id,
                "last_error": r.last_error,
            }
            for r in runs
        ],
        "total": len(runs),
        "limit": limit,
        "offset": offset,
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: str, user: dict = Depends(get_current_user)):
    """Get run detail — state, evidence, approvals, budget, timeline."""
    if _store is None:
        raise HTTPException(503, "Store not configured")

    workspace_id = user["workspace_id"]
    run = await _store.load_latest(run_id, workspace_id)

    if run is None:
        raise HTTPException(404, "Run not found")

    return {
        "run_id": run.run_id,
        "workspace_id": run.workspace_id,
        "lead_id": run.lead_id,
        "state": run.state.value if hasattr(run.state, 'value') else str(run.state),
        "step_id": run.step_id,
        "version": run.version,
        "started_at": run.started_at.isoformat() if hasattr(run.started_at, 'isoformat') else str(run.started_at),
        "updated_at": run.updated_at.isoformat() if hasattr(run.updated_at, 'isoformat') else str(run.updated_at),
        "terminal_at": run.terminal_at.isoformat() if run.terminal_at and hasattr(run.terminal_at, 'isoformat') else None,
        "budget": {
            "tokens_used": run.budget_used_tokens,
            "cost_usd": round(run.budget_used_cost_usd, 6),
            "tool_calls": run.budget_used_tool_calls,
            "max_tokens": run.budget_max_tokens,
            "max_cost_usd": run.budget_max_cost_usd,
        },
        "research": run.research.model_dump() if run.research else None,
        "qualification": run.qualification.model_dump() if run.qualification else None,
        "draft": run.draft.model_dump() if run.draft else None,
        "critic_feedback": run.critic_feedback,
        "pending_approval_id": run.pending_approval_id,
        "draft_attempts": run.draft_attempts,
        "last_error": run.last_error,
        "is_terminal": run.is_terminal(),
        "is_paused": run.is_paused(),
    }


@router.get("/runs/{run_id}/timeline")
async def get_run_timeline(run_id: str, user: dict = Depends(get_current_user)):
    """Get a chronological timeline of events for a run.

    Synthesizes the timeline from the run's current snapshot — the fields
    started_at, updated_at, terminal_at, step_id, draft_attempts,
    pending_approval_id, last_error, and the current state. This is
    honest about what we know: it shows the lifecycle of THIS run from
    creation to its current state, including pauses and retries.

    Future enhancement: store a `run_events` table to capture each
    transition explicitly (state, step_id, actor, timestamp). For v1
    the snapshot-derived timeline is sufficient and accurate.
    """
    if _store is None:
        raise HTTPException(503, "Store not configured")

    workspace_id = user["workspace_id"]
    run = await _store.load_latest(run_id, workspace_id)

    if run is None:
        raise HTTPException(404, "Run not found")

    state_str = run.state.value if hasattr(run.state, 'value') else str(run.state)
    started_iso = run.started_at.isoformat() if hasattr(run.started_at, 'isoformat') else str(run.started_at)
    updated_iso = run.updated_at.isoformat() if hasattr(run.updated_at, 'isoformat') else str(run.updated_at)
    terminal_iso = (
        run.terminal_at.isoformat()
        if run.terminal_at and hasattr(run.terminal_at, 'isoformat')
        else None
    )

    events: list[dict] = []

    # 1. Run created
    events.append({
        "timestamp": started_iso,
        "event": "run_created",
        "step_id": 0,
        "state": "new",
        "detail": f"Run {run.run_id} created for lead {run.lead_id}",
    })

    # 2. Each draft attempt (if any) — counts retries by the Critic
    if run.draft_attempts > 0:
        for attempt in range(1, run.draft_attempts + 1):
            events.append({
                "timestamp": updated_iso,
                "event": "draft_attempt",
                "step_id": run.step_id,
                "state": state_str,
                "detail": f"Draft attempt {attempt} of {run.draft_attempts}",
            })

    # 3. Awaiting approval (Gate 1 or Gate 2)
    if run.pending_approval_id:
        gate = "Gate 2 (email_send)" if state_str == "awaiting_approval" else "Gate 1 (evidence_review)"
        events.append({
            "timestamp": updated_iso,
            "event": "approval_requested",
            "step_id": run.step_id,
            "state": state_str,
            "detail": f"{gate} pending — approval_id={run.pending_approval_id}",
        })

    # 4. SEND_UNKNOWN reconciliation attempts
    if run.reconciliation_attempts > 0:
        events.append({
            "timestamp": updated_iso,
            "event": "send_reconciliation",
            "step_id": run.step_id,
            "state": state_str,
            "detail": (
                f"Email send status unknown — reconciliation attempt "
                f"{run.reconciliation_attempts}"
            ),
        })

    # 5. Last error (if any)
    if run.last_error:
        events.append({
            "timestamp": updated_iso,
            "event": "error",
            "step_id": run.step_id,
            "state": state_str,
            "detail": run.last_error,
        })

    # 6. Terminal event (if terminal)
    if run.is_terminal() and terminal_iso:
        events.append({
            "timestamp": terminal_iso,
            "event": "run_terminal",
            "step_id": run.step_id,
            "state": state_str,
            "detail": f"Run reached terminal state: {state_str}",
        })

    # 7. Latest snapshot (always present)
    events.append({
        "timestamp": updated_iso,
        "event": "snapshot",
        "step_id": run.step_id,
        "state": state_str,
        "detail": (
            f"Current state: {state_str}, version: {run.version}, "
            f"cost: ${run.budget_used_cost_usd:.6f}, "
            f"tokens: {run.budget_used_tokens}"
        ),
    })

    return {
        "run_id": run.run_id,
        "workspace_id": run.workspace_id,
        "lead_id": run.lead_id,
        "current_state": state_str,
        "started_at": started_iso,
        "updated_at": updated_iso,
        "terminal_at": terminal_iso,
        "is_terminal": run.is_terminal(),
        "events": events,
    }


__all__ = ["router", "configure_runs_route"]
