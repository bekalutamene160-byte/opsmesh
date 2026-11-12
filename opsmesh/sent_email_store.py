"""
OPSMESH Sent Email Store — durable log of every email the agent sent.

This is the source of truth for the Activity / Outbox tab. The SendingSpecialist
logs every send attempt here (sent, skipped, failed, unknown) so the operator
can see exactly what was sent, to whom, and when -- without parsing run payloads.

Architecture:
  - SentEmailStoreProtocol: the interface (save, list, get_by_lead, get_by_run)
  - InMemorySentEmailStore: for dev/test (single-process, shared via app.state)
  - The DB-backed version will be added to adapters/db/ when Postgres is configured

This store is SHARED between the API and the in-process worker (passed into
both via app.state), so sends logged by the worker are immediately visible
to the API's /api/v1/activity endpoint.
"""

from __future__ import annotations

from typing import Optional, Protocol

from opsmesh.models import SentEmail


class SentEmailStoreProtocol(Protocol):
    """The interface every sent-email store must implement."""

    async def save(self, email: SentEmail) -> None:
        """Persist a sent-email record."""
        ...

    async def list_recent(
        self, workspace_id: str, limit: int = 50, offset: int = 0
    ) -> tuple[list[SentEmail], int]:
        """List recent sent emails, newest first. Returns (emails, total)."""
        ...

    async def get_by_lead(
        self, workspace_id: str, lead_id: str
    ) -> list[SentEmail]:
        """Get all sent emails for a specific lead."""
        ...

    async def get_by_run(
        self, workspace_id: str, run_id: str
    ) -> list[SentEmail]:
        """Get all sent emails for a specific run."""
        ...


class InMemorySentEmailStore:
    """In-memory sent-email store (dev/test/single-process deployments).

    Thread-safe for asyncio (single-threaded event loop). Stores emails
    in a list, sorted newest-first on read.
    """

    def __init__(self) -> None:
        self._emails: list[SentEmail] = []

    async def save(self, email: SentEmail) -> None:
        self._emails.append(email)

    async def list_recent(
        self, workspace_id: str, limit: int = 50, offset: int = 0
    ) -> tuple[list[SentEmail], int]:
        ws_emails = [e for e in self._emails if e.workspace_id == workspace_id]
        ws_emails.sort(key=lambda e: e.sent_at, reverse=True)
        total = len(ws_emails)
        page = ws_emails[offset : offset + limit]
        return page, total

    async def get_by_lead(
        self, workspace_id: str, lead_id: str
    ) -> list[SentEmail]:
        result = [
            e
            for e in self._emails
            if e.workspace_id == workspace_id and e.lead_id == lead_id
        ]
        result.sort(key=lambda e: e.sent_at, reverse=True)
        return result

    async def get_by_run(
        self, workspace_id: str, run_id: str
    ) -> list[SentEmail]:
        result = [
            e
            for e in self._emails
            if e.workspace_id == workspace_id and e.run_id == run_id
        ]
        result.sort(key=lambda e: e.sent_at, reverse=True)
        return result


__all__ = ["SentEmailStoreProtocol", "InMemorySentEmailStore"]
