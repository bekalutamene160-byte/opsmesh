"""
OPSMESH Lead Store — durable persistence for lead records.

Phase 7 architectural fix:
  Before Phase 7, LeadIntakeService kept leads in an in-memory dict
  (`_leads`). This was fine for tests, but in production:
    - Leads disappear on restart
    - Leads created by the API aren't visible to the worker
    - The /api/v1/leads list endpoint can only see leads from THIS process

  This module adds a proper LeadStore that persists to the database
  (SQLite or Postgres — same connection abstraction). LeadIntakeService
  now delegates lead storage to this store.

Architecture:
  - InMemoryLeadStore  — for tests + dev mode (no DB configured)
  - DatabaseLeadStore   — for production (uses get_connection)

  Both implement the same LeadStore Protocol.
"""

from __future__ import annotations

import json
from typing import Optional, Protocol

from opsmesh.adapters.db.connection import get_connection, get_db_backend
from opsmesh.models import Lead, LeadDisposition, LeadSource, utcnow


# ---------------------------------------------------------------------------
# LeadStore Protocol
# ---------------------------------------------------------------------------


class LeadStore(Protocol):
    """The lead persistence contract."""

    async def save(self, lead: Lead) -> None:
        """Insert or update a lead (upsert by id)."""
        ...

    async def get(self, workspace_id: str, lead_id: str) -> Optional[Lead]:
        """Fetch a lead. Returns None if not found OR cross-workspace."""
        ...

    async def list_leads(
        self,
        workspace_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Lead], int]:
        """List leads for a workspace, sorted by created_at descending.

        Returns (page, total_count).
        """
        ...

    async def update_disposition(
        self,
        workspace_id: str,
        lead_id: str,
        disposition: LeadDisposition,
        reason: Optional[str] = None,
    ) -> None:
        """Update a lead's disposition (e.g., new → contacted)."""
        ...

    async def find_by_submission(
        self, workspace_id: str, submission_id: str
    ) -> Optional[Lead]:
        """Look up a lead by (workspace_id, source_submission_id).

        Used for webhook dedup. Returns None if not found.
        """
        ...


# ---------------------------------------------------------------------------
# InMemoryLeadStore — for tests + dev mode
# ---------------------------------------------------------------------------


class InMemoryLeadStore:
    """In-memory lead store. Mirror of the database contract.

    Thread-safety: asyncio is single-threaded within one event loop,
    so plain dict access is safe here. For multi-process tests, use
    DatabaseLeadStore.
    """

    def __init__(self) -> None:
        self._leads: dict[tuple[str, str], Lead] = {}
        self._by_submission: dict[tuple[str, str], Lead] = {}

    async def save(self, lead: Lead) -> None:
        self._leads[(lead.workspace_id, lead.id)] = lead
        if lead.source_submission_id:
            self._by_submission[(lead.workspace_id, lead.source_submission_id)] = lead

    async def get(self, workspace_id: str, lead_id: str) -> Optional[Lead]:
        return self._leads.get((workspace_id, lead_id))

    async def list_leads(
        self,
        workspace_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Lead], int]:
        ws_leads = [l for (ws, _), l in self._leads.items() if ws == workspace_id]
        ws_leads.sort(key=lambda l: l.created_at, reverse=True)
        total = len(ws_leads)
        return ws_leads[offset:offset + limit], total

    async def update_disposition(
        self,
        workspace_id: str,
        lead_id: str,
        disposition: LeadDisposition,
        reason: Optional[str] = None,
    ) -> None:
        lead = self._leads.get((workspace_id, lead_id))
        if lead is None:
            return
        lead.disposition = disposition
        lead.disposition_reason = reason
        lead.updated_at = utcnow()

    async def find_by_submission(
        self, workspace_id: str, submission_id: str
    ) -> Optional[Lead]:
        return self._by_submission.get((workspace_id, submission_id))


# ---------------------------------------------------------------------------
# DatabaseLeadStore — SQLite + Postgres (via the unified connection)
# ---------------------------------------------------------------------------


class DatabaseLeadStore:
    """Database-backed lead store. Works with SQLite or Postgres.

    Uses the get_connection() abstraction from connection.py. The SQL
    is compatible with both backends (uses ? placeholders, which the
    Postgres adapter converts to $1, $2, ... automatically).
    """

    async def save(self, lead: Lead) -> None:
        async with get_connection() as conn:
            # Upsert: try INSERT first, fall back to UPDATE if it exists
            # (We use INSERT OR IGNORE for SQLite, and check existing first
            # for both backends — simpler than ON CONFLICT clauses that
            # differ between SQLite and Postgres.)
            cursor = await conn.execute(
                "SELECT id FROM leads WHERE id = ?",
                (lead.id,),
            )
            existing = await cursor.fetchone()
            await cursor.close()

            if existing:
                await conn.execute(
                    """UPDATE leads SET
                        workspace_id = ?, crm_lead_id = ?, company = ?,
                        contact_email = ?, contact_name = ?, industry = ?,
                        employees = ?, website = ?, lead_type = ?, source = ?,
                        source_submission_id = ?, disposition = ?,
                        disposition_reason = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        lead.workspace_id, lead.crm_lead_id, lead.company,
                        lead.contact_email, lead.contact_name, lead.industry,
                        lead.employees, lead.website, lead.lead_type,
                        lead.source.value if hasattr(lead.source, "value") else str(lead.source),
                        lead.source_submission_id,
                        lead.disposition.value if hasattr(lead.disposition, "value") else str(lead.disposition),
                        lead.disposition_reason,
                        lead.updated_at.isoformat(),
                        lead.id,
                    ),
                )
            else:
                await conn.execute(
                    """INSERT INTO leads (
                        id, workspace_id, crm_lead_id, company, contact_email,
                        contact_name, industry, employees, website, lead_type, source,
                        source_submission_id, disposition, disposition_reason,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        lead.id, lead.workspace_id, lead.crm_lead_id, lead.company,
                        lead.contact_email, lead.contact_name, lead.industry,
                        lead.employees, lead.website, lead.lead_type,
                        lead.source.value if hasattr(lead.source, "value") else str(lead.source),
                        lead.source_submission_id,
                        lead.disposition.value if hasattr(lead.disposition, "value") else str(lead.disposition),
                        lead.disposition_reason,
                        lead.created_at.isoformat(),
                        lead.updated_at.isoformat(),
                    ),
                )
            await conn.commit()

    async def get(self, workspace_id: str, lead_id: str) -> Optional[Lead]:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT id, workspace_id, crm_lead_id, company, contact_email, "
                "contact_name, industry, employees, website, lead_type, source, "
                "source_submission_id, disposition, disposition_reason, "
                "created_at, updated_at "
                "FROM leads WHERE id = ? AND workspace_id = ?",
                (lead_id, workspace_id),
            )
            row = await cursor.fetchone()
            await cursor.close()

            if row is None:
                return None  # not found OR cross-workspace — both treated as 404

            return _row_to_lead(row)

    async def list_leads(
        self,
        workspace_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Lead], int]:
        async with get_connection() as conn:
            # Get total count
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM leads WHERE workspace_id = ?",
                (workspace_id,),
            )
            total_row = await cursor.fetchone()
            await cursor.close()
            total = total_row[0] if total_row else 0

            # Get the page
            cursor = await conn.execute(
                "SELECT id, workspace_id, crm_lead_id, company, contact_email, "
                "contact_name, industry, employees, website, lead_type, source, "
                "source_submission_id, disposition, disposition_reason, "
                "created_at, updated_at "
                "FROM leads WHERE workspace_id = ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (workspace_id, limit, offset),
            )
            rows = await cursor.fetchall()
            await cursor.close()

            leads = [_row_to_lead(r) for r in rows]
            return leads, total

    async def update_disposition(
        self,
        workspace_id: str,
        lead_id: str,
        disposition: LeadDisposition,
        reason: Optional[str] = None,
    ) -> None:
        async with get_connection() as conn:
            await conn.execute(
                """UPDATE leads SET
                    disposition = ?, disposition_reason = ?, updated_at = ?
                   WHERE id = ? AND workspace_id = ?""",
                (
                    disposition.value if hasattr(disposition, "value") else str(disposition),
                    reason,
                    utcnow().isoformat(),
                    lead_id,
                    workspace_id,
                ),
            )
            await conn.commit()

    async def find_by_submission(
        self, workspace_id: str, submission_id: str
    ) -> Optional[Lead]:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT id, workspace_id, crm_lead_id, company, contact_email, "
                "contact_name, industry, employees, website, lead_type, source, "
                "source_submission_id, disposition, disposition_reason, "
                "created_at, updated_at "
                "FROM leads WHERE workspace_id = ? AND source_submission_id = ?",
                (workspace_id, submission_id),
            )
            row = await cursor.fetchone()
            await cursor.close()

            if row is None:
                return None
            return _row_to_lead(row)


# ---------------------------------------------------------------------------
# Helper: convert a DB row to a Lead object
# ---------------------------------------------------------------------------


def _row_to_lead(row) -> Lead:
    """Convert a DB row (16 columns) to a Lead Pydantic model.

    Handles datetime parsing from ISO strings.
    """
    from datetime import datetime

    def _parse_dt(s):
        if not s:
            return utcnow()
        try:
            return datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return utcnow()

    return Lead(
        id=row[0],
        workspace_id=row[1],
        crm_lead_id=row[2],
        company=row[3],
        contact_email=row[4],
        contact_name=row[5],
        industry=row[6],
        employees=row[7],
        website=row[8],
        lead_type=row[9] or "company",
        source=LeadSource(row[10]),
        source_submission_id=row[11],
        disposition=LeadDisposition(row[12]),
        disposition_reason=row[13],
        created_at=_parse_dt(row[14]),
        updated_at=_parse_dt(row[15]),
    )


# ---------------------------------------------------------------------------
# Factory: returns the right LeadStore based on the configured backend
# ---------------------------------------------------------------------------


def create_lead_store() -> LeadStore:
    """Return the appropriate LeadStore based on the configured backend.

    - In-memory (dev/test): InMemoryLeadStore
    - SQLite or Postgres: DatabaseLeadStore
    """
    backend = get_db_backend()
    if backend == "memory":
        return InMemoryLeadStore()
    return DatabaseLeadStore()


__all__ = [
    "LeadStore",
    "InMemoryLeadStore",
    "DatabaseLeadStore",
    "create_lead_store",
]
