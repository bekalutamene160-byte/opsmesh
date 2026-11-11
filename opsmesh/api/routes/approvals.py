"""
OPSMESH Approvals API — human-facing approval endpoints.

Endpoints:
  GET  /api/v1/approvals          — list pending approvals
  GET  /api/v1/approvals/{id}     — get approval detail
  POST /api/v1/approvals/{id}/decision — submit a decision

Architecture:
  These endpoints wrap the ApprovalGate from Day 5. The human accesses
  them through the frontend (or curl). Each endpoint:
    1. Authenticates the user (Supabase JWT)
    2. Verifies workspace ownership
    3. Calls the ApprovalGate
    4. Returns the result

  The approval_id is passed in the URL (not the body). The decision
  is in the request body. The workspace_id comes from the authenticated
  user (never from the request).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from opsmesh.api.auth import get_current_user
from opsmesh.approval_gate import (
    AlreadyResolvedError,
    ApprovalGate,
    InvalidDecisionError,
)
from opsmesh.approval_store import ApprovalNotFoundError
from opsmesh.models import ApprovalKind, ApprovalStatus


router = APIRouter(prefix="/api/v1", tags=["approvals"])

# Module-level references — set by configure_approvals_route()
_gate: Optional[ApprovalGate] = None
_queue = None  # The job queue (for re-enqueuing runs after a decision)


def configure_approvals_route(gate: ApprovalGate, queue=None) -> None:
    """Configure the approvals route with the given ApprovalGate + queue.

    Args:
        gate: The ApprovalGate (for creating/deciding approvals).
        queue: Optional JobQueue (for re-enqueuing runs after a decision).
            When the user approves/rejects, we enqueue a new job so the
            worker picks it up and the supervisor resumes the run.
    """
    global _gate, _queue
    _gate = gate
    _queue = queue


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class DecisionRequest(BaseModel):
    """Human submits a decision on an approval."""
    decision: str = Field(
        ...,
        description="Gate 1: proceed_with_limitations | request_more_info | stop. "
                    "Gate 2: approve | reject | request_changes",
    )
    feedback: Optional[str] = Field(None, max_length=1000)


class ApprovalResponse(BaseModel):
    """Approval detail response."""
    id: str
    run_id: str
    kind: str
    status: str
    action: str
    decision: Optional[str] = None
    decision_feedback: Optional[str] = None
    decided_by: Optional[str] = None
    created_at: str
    expires_at: str


class DecisionResponse(BaseModel):
    """Decision submission response."""
    approval_id: str
    decision: str
    status: str
    decided_at: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/approvals")
async def list_approvals(user: dict = Depends(get_current_user)):
    """List pending approvals for the authenticated user's workspace."""
    if _gate is None:
        raise HTTPException(503, "Approval gate not configured")

    workspace_id = user["workspace_id"]

    # Use the store's list_pending() method (works for both InMemoryApprovalStore
    # and SQLiteApprovalStore). This is the fix for the "no approvals" bug:
    # the API and worker now share the same DB-backed approval store, so
    # approvals created by the worker are visible to the API.
    approval_store = _gate.approval_store
    if hasattr(approval_store, "list_pending"):
        # Preferred path — works for both InMemory and SQLite/Postgres stores
        records = approval_store.list_pending(workspace_id)
    else:
        # Fallback: scan the internal dict (InMemoryApprovalStore only)
        records = [
            record for record in getattr(approval_store, "_approvals", {}).values()
            if record.workspace_id == workspace_id
            and record.status == ApprovalStatus.PENDING
        ]

    pending = [
        ApprovalResponse(
            id=record.id,
            run_id=record.run_id,
            kind=record.kind.value if hasattr(record.kind, 'value') else str(record.kind),
            status=record.status.value if hasattr(record.status, 'value') else str(record.status),
            action=record.action,
            decision=record.decision,
            decision_feedback=record.decision_feedback,
            decided_by=record.decided_by,
            created_at=record.created_at.isoformat() if hasattr(record.created_at, 'isoformat') else str(record.created_at),
            expires_at=record.expires_at.isoformat() if hasattr(record.expires_at, 'isoformat') else str(record.expires_at),
        )
        for record in records
    ]

    return {"approvals": [a.model_dump() for a in pending]}


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str, user: dict = Depends(get_current_user)):
    """Get approval detail."""
    if _gate is None:
        raise HTTPException(503, "Approval gate not configured")

    record = _gate.approval_store.get(approval_id)
    if record is None:
        raise HTTPException(404, "Approval not found")

    if record.workspace_id != user["workspace_id"]:
        raise HTTPException(403, "Access denied: approval belongs to a different workspace")

    return ApprovalResponse(
        id=record.id,
        run_id=record.run_id,
        kind=record.kind.value if hasattr(record.kind, 'value') else str(record.kind),
        status=record.status.value if hasattr(record.status, 'value') else str(record.status),
        action=record.action,
        decision=record.decision,
        decision_feedback=record.decision_feedback,
        decided_by=record.decided_by,
        created_at=record.created_at.isoformat() if hasattr(record.created_at, 'isoformat') else str(record.created_at),
        expires_at=record.expires_at.isoformat() if hasattr(record.expires_at, 'isoformat') else str(record.expires_at),
    ).model_dump()


@router.post("/approvals/{approval_id}/decision")
async def submit_decision(
    approval_id: str,
    req: DecisionRequest,
    user: dict = Depends(get_current_user),
):
    """Submit a human decision on an approval.

    Gate 1 decisions: proceed_with_limitations, request_more_info, stop
    Gate 2 decisions: approve, reject, request_changes

    After the decision is recorded, a new job is enqueued so the worker
    picks it up and the supervisor resumes the run (transitioning from
    the paused state to the next step).
    """
    if _gate is None:
        raise HTTPException(503, "Approval gate not configured")

    try:
        record = await _gate.decide(
            approval_id=approval_id,
            workspace_id=user["workspace_id"],
            decision=req.decision,
            decided_by=user["user_id"],
            feedback=req.feedback,
        )

        # Re-enqueue a job for the run so the worker resumes it.
        # Without this, the run stays paused forever — the worker only
        # polls for new jobs, it doesn't know the approval was decided.
        if _queue is not None:
            try:
                await _queue.enqueue(record.workspace_id, record.run_id)
            except Exception as e:
                # Log but don't fail the decision — the approval IS decided,
                # the operator can manually resume the run if needed.
                import logging
                logging.getLogger("opsmesh.api.approvals").warning(
                    f"Failed to re-enqueue run {record.run_id} after decision: {e}"
                )

        return DecisionResponse(
            approval_id=record.id,
            decision=record.decision or "",
            status=record.status.value if hasattr(record.status, 'value') else str(record.status),
            decided_at=record.decided_at.isoformat() if record.decided_at and hasattr(record.decided_at, 'isoformat') else str(record.decided_at),
        )

    except ApprovalNotFoundError:
        raise HTTPException(404, "Approval not found")
    except AlreadyResolvedError as e:
        raise HTTPException(409, f"Already resolved: {e.message}")
    except InvalidDecisionError as e:
        raise HTTPException(422, str(e))


__all__ = ["router", "configure_approvals_route"]
