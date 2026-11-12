"""
OPSMESH Activity Routes — the Activity / Outbox tab.

Endpoints:
  GET /api/v1/activity          - combined view: recent sent emails + history events
  GET /api/v1/activity/emails   - sent emails (the outbox)
  GET /api/v1/activity/history  - recent history events (the agent's memory)
  GET /api/v1/activity/leads/{lead_id} - all activity for a specific lead

The Activity tab shows what the agent has done: sent emails (with full
content, recipient, status), research summaries, and the history of every
pipeline step. This is the "portfolio-ready" view that proves the agent
is working end-to-end.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from opsmesh.api.auth import get_current_user

router = APIRouter(prefix="/api/v1", tags=["activity"])

# Module-level references set by configure_activity_routes()
_sent_email_store = None
_history_store = None
_workspace_id = "default"


def configure_activity_routes(sent_email_store=None, history_store=None,
                              workspace_id: str = "default") -> None:
    """Wire up the activity route with the shared stores.

    The stores MUST be the same instances the API and in-process worker
    share (set on app.state in create_app()). This ensures sent emails
    logged by the worker's SendingSpecialist are immediately visible here.
    """
    global _sent_email_store, _history_store, _workspace_id
    _sent_email_store = sent_email_store
    _history_store = history_store
    _workspace_id = workspace_id


@router.get("/activity")
async def get_activity(
    user: dict = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Combined activity view: recent sent emails + history events."""
    ws = user.get("workspace_id", _workspace_id)
    emails = []
    events = []
    if _sent_email_store is not None:
        em_list, em_total = await _sent_email_store.list_recent(ws, limit=limit)
        emails = [_serialize_email(e) for e in em_list]
    if _history_store is not None:
        ev_list, ev_total = await _history_store.list_recent(ws, limit=limit)
        events = [_serialize_event(e) for e in ev_list]
    return {
        "sent_emails": emails,
        "history_events": events,
        "stats": {
            "total_sent": len(emails),
            "total_events": len(events),
        },
    }


@router.get("/activity/emails")
async def list_sent_emails(
    user: dict = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List sent emails (the outbox). Newest first."""
    ws = user.get("workspace_id", _workspace_id)
    if _sent_email_store is None:
        return {"emails": [], "total": 0}
    emails, total = await _sent_email_store.list_recent(ws, limit=limit, offset=offset)
    return {
        "emails": [_serialize_email(e) for e in emails],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/activity/history")
async def list_history(
    user: dict = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List recent history events (the agent's memory). Newest first."""
    ws = user.get("workspace_id", _workspace_id)
    if _history_store is None:
        return {"events": [], "total": 0}
    events, total = await _history_store.list_recent(ws, limit=limit, offset=offset)
    return {
        "events": [_serialize_event(e) for e in events],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/activity/leads/{lead_id}")
async def get_lead_activity(
    lead_id: str,
    user: dict = Depends(get_current_user),
):
    """All activity for a specific lead: sent emails + history events."""
    ws = user.get("workspace_id", _workspace_id)
    emails = []
    events = []
    if _sent_email_store is not None:
        em_list = await _sent_email_store.get_by_lead(ws, lead_id)
        emails = [_serialize_email(e) for e in em_list]
    if _history_store is not None:
        ev_list = await _history_store.list_by_lead(ws, lead_id)
        events = [_serialize_event(e) for e in ev_list]
    return {
        "lead_id": lead_id,
        "sent_emails": emails,
        "history_events": events,
    }


def _serialize_email(e) -> dict:
    return {
        "id": e.id,
        "lead_id": e.lead_id,
        "run_id": e.run_id,
        "recipient": e.recipient,
        "from_address": e.from_address,
        "subject": e.subject,
        "body": e.body,
        "status": e.status,
        "provider_message_id": e.provider_message_id,
        "lead_type": e.lead_type,
        "sent_at": e.sent_at.isoformat() if e.sent_at else None,
    }


def _serialize_event(e) -> dict:
    return {
        "id": e.id,
        "lead_id": e.lead_id,
        "run_id": e.run_id,
        "event_type": e.event_type,
        "summary": e.summary,
        "detail": e.detail,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }
