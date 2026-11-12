"""
OPSMESH Leads Routes — manual lead creation + lead inspection.

Endpoints:
  GET  /api/v1/leads              — list leads (with pagination) [Phase 4]
  POST /api/v1/leads              — manual lead creation (FR-1)
  GET  /api/v1/leads/{lead_id}    — lead detail (FR-1)
  POST /api/v1/demo/simulate     — simulated lead event (FR-2)

All three intake paths invoke the same LeadIntakeService.create_lead (FR-5).

Architecture (Phase 4):
  Module-level state pattern (same as runs.py, dashboard.py, metrics.py).
  The `configure_leads_routes()` function sets module-level variables
  `_intake_service` and `_workspace_id`. The route handlers read these
  at request time, so they always see the latest configured values.

  The previous closure pattern captured `_intake_service` at first
  configure call, which broke when `create_app()` was called more than
  once (tests, multi-app scenarios).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field

from opsmesh.api.auth import get_current_user
from opsmesh.api.rate_limit import LEAD_WRITE_LIMIT, SIMULATE_LIMIT, limiter
from opsmesh.models import LeadSource
from opsmesh.services.lead_intake import (
    DuplicateSubmission,
    LeadIntakeService,
    SuppressedEmail,
)


router = APIRouter(prefix="/api/v1", tags=["leads"])

# Module-level state — set by configure_leads_routes()
_intake_service: Optional[LeadIntakeService] = None
_workspace_id: str = "default"
_store = None
_queue = None
_lead_store = None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateLeadRequest(BaseModel):
    """Manual lead creation payload (POST /api/v1/leads).

    Supports both company leads (lead_type='company', company required)
    and individual leads (lead_type='individual', company may be empty).
    """
    lead_type: str = Field(default="company", max_length=20)
    company: str = Field(default="", max_length=200)
    contact_email: EmailStr
    contact_name: Optional[str] = Field(default=None, max_length=200)
    industry: Optional[str] = None
    employees: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    website: Optional[str] = None
    notes: Optional[str] = None


class CreateLeadResponse(BaseModel):
    """201 Created response for manual lead creation."""
    lead_id: str
    run_id: str
    status: str = "queued"


class LeadDetailResponse(BaseModel):
    """Lead detail (GET /api/v1/leads/{lead_id})."""
    id: str
    lead_type: str = "company"
    company: str = ""
    contact_email: str
    contact_name: Optional[str] = None
    industry: Optional[str] = None
    employees: Optional[int] = None
    website: Optional[str] = None
    notes: Optional[str] = None
    score: Optional[int] = None
    source: str
    disposition: str
    created_at: str


class LeadListItem(BaseModel):
    """One row in the leads list (GET /api/v1/leads)."""
    id: str
    lead_type: str = "company"
    company: str = ""
    contact_email: str
    contact_name: Optional[str] = None
    industry: Optional[str] = None
    score: Optional[int] = None
    source: str
    disposition: str
    created_at: str


class SimulateRequest(BaseModel):
    """Simulated lead event (POST /api/v1/demo/simulate).

    If no body or empty body, picks a synthetic lead from the seeded dataset.
    """
    company: Optional[str] = None
    contact_email: Optional[EmailStr] = None


class SimulateResponse(BaseModel):
    lead_id: str
    run_id: str
    scenario: str = "simulated"
    status: str = "queued"


# ---------------------------------------------------------------------------
# Route handlers — module-level (Phase 4 refactor)
# ---------------------------------------------------------------------------


def configure_leads_routes(
    intake_service: LeadIntakeService,
    default_workspace_id: str = "default",
    store=None,
    queue=None,
    lead_store=None,
) -> None:
    """Configure the leads routes with the given service.

    Sets module-level state that the route handlers read at request time.
    Safe to call multiple times (e.g., across test apps); each call updates
    the module-level state, and existing apps immediately see the new
    values.

    Stage B Phase 8: store/queue/lead_store are used by the demo reset
    endpoint to clear demo data for a fresh demo.
    """
    global _intake_service, _workspace_id, _store, _queue, _lead_store
    _intake_service = intake_service
    _workspace_id = default_workspace_id
    _store = store
    _queue = queue
    _lead_store = lead_store


@router.get("/leads", tags=["leads"])
async def list_leads(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List leads for the authenticated user's workspace.

    Returns leads sorted by created_at descending (newest first).
    Pagination via limit/offset. The `total` field gives the full
    workspace count for pagination UI.
    """
    if _intake_service is None:
        raise HTTPException(503, "Lead intake service not configured")

    workspace_id = user["workspace_id"]
    leads_page, total = await _intake_service.list_leads(
        workspace_id=workspace_id,
        limit=limit,
        offset=offset,
    )
    return {
        "leads": [
            LeadListItem(
                id=lead.id,
                lead_type=lead.lead_type,
                company=lead.company,
                contact_email=lead.contact_email,
                contact_name=lead.contact_name,
                industry=lead.industry,
                score=lead.score,
                source=lead.source.value,
                disposition=lead.disposition.value,
                created_at=lead.created_at.isoformat(),
            ).model_dump()
            for lead in leads_page
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/leads", status_code=201, response_model=CreateLeadResponse)
@limiter.limit(LEAD_WRITE_LIMIT)
async def create_lead_manual(req: CreateLeadRequest, request: Request):
    """Manual lead creation (FR-1). Creates a lead + run + job.

    Stage B Phase 1 (P0-5): rate-limited per-IP to prevent cost abuse.
    """
    if _intake_service is None:
        raise HTTPException(503, "Lead intake service not configured")

    try:
        result = await _intake_service.create_lead(
            workspace_id=_workspace_id,
            lead_type=req.lead_type,
            company=req.company,
            contact_email=str(req.contact_email),
            contact_name=req.contact_name,
            industry=req.industry,
            employees=req.employees,
            website=req.website,
            notes=req.notes,
            source=LeadSource.MANUAL,
        )
    except SuppressedEmail:
        raise HTTPException(
            status_code=403,
            detail="Email is on the suppression list.",
        )
    except DuplicateSubmission as e:
        raise HTTPException(
            status_code=409,
            detail=f"Duplicate submission_id: {e.submission_id}",
        )
    return CreateLeadResponse(lead_id=result.lead_id, run_id=result.run_id)


@router.get("/leads/{lead_id}", response_model=LeadDetailResponse)
async def get_lead(lead_id: str):
    """Get lead detail (FR-1). Returns 404 if not found."""
    if _intake_service is None:
        raise HTTPException(503, "Lead intake service not configured")

    lead = await _intake_service.get_lead(_workspace_id, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return LeadDetailResponse(
        id=lead.id,
        lead_type=lead.lead_type,
        company=lead.company,
        contact_email=lead.contact_email,
        contact_name=lead.contact_name,
        industry=lead.industry,
        employees=lead.employees,
        website=lead.website,
        notes=lead.notes,
        score=lead.score,
        source=lead.source.value,
        disposition=lead.disposition.value,
        created_at=lead.created_at.isoformat(),
    )


@router.post("/demo/simulate", status_code=202, response_model=SimulateResponse)
@limiter.limit(SIMULATE_LIMIT)
async def simulate_lead(request: Request, req: Optional[SimulateRequest] = None):
    """Simulated lead event (FR-2). Picks a synthetic lead or uses
    the provided one. Always uses LeadSource.SIMULATED.

    Stage B Phase 1 (P0-5): stricter rate limit (5/min) since this endpoint
    fabricates leads from no signed input.
    """
    if _intake_service is None:
        raise HTTPException(503, "Lead intake service not configured")

    # If no body, generate a synthetic lead
    if req is None or req.company is None:
        company = "Simulated Corp"
        email = "sim@example.com"
    else:
        company = req.company
        email = str(req.contact_email) if req.contact_email else "sim@example.com"

    try:
        result = await _intake_service.create_lead(
            workspace_id=_workspace_id,
            company=company,
            contact_email=email,
            source=LeadSource.SIMULATED,
        )
    except SuppressedEmail:
        raise HTTPException(
            status_code=403,
            detail="Email is on the suppression list.",
        )

    return SimulateResponse(lead_id=result.lead_id, run_id=result.run_id)


@router.post("/demo/reset", status_code=200, tags=["demo"])
@limiter.limit(SIMULATE_LIMIT)
async def demo_reset(request: Request):
    """Stage B Phase 8: clear all demo data for a fresh demo run.

    Wipes runs, jobs, leads, and (for in-memory stores) approvals +
    sent-email/history logs in the bound workspace. Only available in
    single_user_demo mode (the demo reset is a demo-only convenience; it
    must NOT be exposed in multi_user prod where it would destroy real data).

    Returns a summary of what was cleared.
    """
    from opsmesh.config import is_demo_mode, get_settings
    if not is_demo_mode(get_settings()):
        raise HTTPException(403, "Demo reset is only available in single_user_demo mode.")
    ws = _workspace_id
    cleared = {"workspace": ws, "runs": 0, "jobs": 0, "leads": 0}

    # Clear runs (in-memory: _runs dict; SQLite: DELETE).
    if _store is not None:
        try:
            if hasattr(_store, "_runs"):
                n = len([r for r in _store._runs.values() if r.workspace_id == ws])
                _store._runs = {k: v for k, v in _store._runs.items() if v.workspace_id != ws}
                cleared["runs"] = n
            elif hasattr(_store, "_db_path"):
                import sqlite3 as _sq
                from opsmesh.adapters.db.connection import get_db_path
                conn = _sq.connect(get_db_path())
                conn.execute("DELETE FROM runs WHERE workspace_id = ?", (ws,))
                conn.commit()
                cleared["runs"] = conn.total_changes
                conn.close()
        except Exception:
            pass

    # Clear jobs.
    if _queue is not None:
        try:
            if hasattr(_queue, "_jobs"):
                n = len([j for j in _queue._jobs.values() if getattr(j, "workspace_id", "") == ws])
                _queue._jobs = {k: v for k, v in _queue._jobs.items() if getattr(v, "workspace_id", "") != ws}
                cleared["jobs"] = n
        except Exception:
            pass

    # Clear leads.
    if _lead_store is not None:
        try:
            if hasattr(_lead_store, "_leads"):
                n = len([l for l in _lead_store._leads.values() if getattr(l, "workspace_id", "") == ws])
                _lead_store._leads = {k: v for k, v in _lead_store._leads.items() if getattr(v, "workspace_id", "") != ws}
                cleared["leads"] = n
        except Exception:
            pass

    return {"status": "reset", "cleared": cleared}


__all__ = [
    "router",
    "configure_leads_routes",
    "CreateLeadRequest",
    "CreateLeadResponse",
    "LeadDetailResponse",
    "LeadListItem",
    "SimulateRequest",
    "SimulateResponse",
]
