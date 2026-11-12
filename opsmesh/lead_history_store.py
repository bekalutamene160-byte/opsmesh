"""
OPSMESH Lead History Store — the agent's durable memory of interactions.

Every pipeline step (research, qualify, draft, critic, approve, send,
crm_update, reject, create) logs one LeadHistoryEvent here. This gives
the agent a memory of what it did with each lead, visible in the Activity
tab and on the lead detail page.

When a lead re-enters the pipeline (e.g., a follow-up run), past events
are available as context -- the agent "remembers the user's history."

Architecture:
  - LeadHistoryStoreProtocol: the interface (save, list_by_lead, list_recent)
  - InMemoryLeadHistoryStore: for dev/test (single-process, shared via app.state)

This store is SHARED between the API and the in-process worker, so events
logged by the worker's specialists are immediately visible to the API.
"""

from __future__ import annotations

from typing import Optional, Protocol

from opsmesh.models import LeadHistoryEvent


class LeadHistoryStoreProtocol(Protocol):
    """The interface every lead-history store must implement."""

    async def save(self, event: LeadHistoryEvent) -> None:
        """Persist a history event."""
        ...

    async def list_by_lead(
        self, workspace_id: str, lead_id: str, limit: int = 50
    ) -> list[LeadHistoryEvent]:
        """Get all history events for a lead, newest first."""
        ...

    async def list_recent(
        self, workspace_id: str, limit: int = 50, offset: int = 0
    ) -> tuple[list[LeadHistoryEvent], int]:
        """List recent events across all leads, newest first."""
        ...


class InMemoryLeadHistoryStore:
    """In-memory lead-history store (dev/test/single-process deployments)."""

    def __init__(self) -> None:
        self._events: list[LeadHistoryEvent] = []

    async def save(self, event: LeadHistoryEvent) -> None:
        self._events.append(event)

    async def list_by_lead(
        self, workspace_id: str, lead_id: str, limit: int = 50
    ) -> list[LeadHistoryEvent]:
        result = [
            e
            for e in self._events
            if e.workspace_id == workspace_id and e.lead_id == lead_id
        ]
        result.sort(key=lambda e: e.created_at, reverse=True)
        return result[:limit]

    async def list_recent(
        self, workspace_id: str, limit: int = 50, offset: int = 0
    ) -> tuple[list[LeadHistoryEvent], int]:
        ws_events = [e for e in self._events if e.workspace_id == workspace_id]
        ws_events.sort(key=lambda e: e.created_at, reverse=True)
        total = len(ws_events)
        page = ws_events[offset : offset + limit]
        return page, total


__all__ = ["LeadHistoryStoreProtocol", "InMemoryLeadHistoryStore"]
