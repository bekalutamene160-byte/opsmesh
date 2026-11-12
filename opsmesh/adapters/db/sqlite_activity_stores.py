"""OPSMESH SQLite activity stores — Stage B Phase 4 durable persistence.

Implements the SentEmailStoreProtocol, LeadHistoryStoreProtocol, a
DB-backed contact-policy (suppression) store, and a DB-backed AuditLogger.
Each persists to SQLite (or Postgres via the unified connection abstraction)
so the Activity tab, lead memory, suppression list, and audit trail survive
process restarts.

When no database is configured (dev/test/in-memory mode), the in-memory
stores in ``sent_email_store.py`` / ``lead_history_store.py`` /
``security_context.py`` are used instead. The factory
``create_activity_stores()`` picks the right implementation.

Pattern: synchronous sqlite3 calls inside async methods (single-row inserts
+ simple queries — fast enough that blocking the event loop briefly is
acceptable, matching the existing SQLiteOutboxStore / SQLiteApprovalStore).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Optional

from opsmesh.adapters.db.connection import get_db_path
from opsmesh.models import LeadHistoryEvent, SentEmail, utcnow


def _connect() -> sqlite3.Connection:
    db_path = get_db_path()
    if db_path is None:
        raise RuntimeError("Database not initialized — cannot use SQLite activity store")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _iso(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.isoformat()


# ---------------------------------------------------------------------------
# SQLiteSentEmailStore
# ---------------------------------------------------------------------------


class SQLiteSentEmailStore:
    """Durable sent-email log backed by the ``sent_emails`` table."""

    async def save(self, email: SentEmail) -> None:
        conn = _connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO sent_emails
                   (id, workspace_id, lead_id, run_id, recipient, from_address,
                    subject, body, status, provider_message_id, lead_type, sent_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    email.id, email.workspace_id, email.lead_id, email.run_id,
                    email.recipient, email.from_address, email.subject, email.body,
                    email.status, email.provider_message_id, email.lead_type,
                    _iso(email.sent_at),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def list_recent(
        self, workspace_id: str, limit: int = 50, offset: int = 0
    ) -> tuple[list[SentEmail], int]:
        conn = _connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM sent_emails WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()[0]
            rows = conn.execute(
                """SELECT * FROM sent_emails WHERE workspace_id = ?
                   ORDER BY sent_at DESC LIMIT ? OFFSET ?""",
                (workspace_id, limit, offset),
            ).fetchall()
            return [self._row_to_model(r) for r in rows], total
        finally:
            conn.close()

    async def get_by_lead(self, workspace_id: str, lead_id: str) -> list[SentEmail]:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT * FROM sent_emails WHERE workspace_id = ? AND lead_id = ?
                   ORDER BY sent_at DESC""",
                (workspace_id, lead_id),
            ).fetchall()
            return [self._row_to_model(r) for r in rows]
        finally:
            conn.close()

    async def get_by_run(self, workspace_id: str, run_id: str) -> list[SentEmail]:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT * FROM sent_emails WHERE workspace_id = ? AND run_id = ?
                   ORDER BY sent_at DESC""",
                (workspace_id, run_id),
            ).fetchall()
            return [self._row_to_model(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> SentEmail:
        return SentEmail(
            id=row["id"],
            workspace_id=row["workspace_id"],
            lead_id=row["lead_id"],
            run_id=row["run_id"],
            recipient=row["recipient"],
            from_address=row["from_address"],
            subject=row["subject"],
            body=row["body"],
            status=row["status"],
            provider_message_id=row["provider_message_id"],
            lead_type=row["lead_type"],
            sent_at=datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else utcnow(),
        )


# ---------------------------------------------------------------------------
# SQLiteLeadHistoryStore
# ---------------------------------------------------------------------------


class SQLiteLeadHistoryStore:
    """Durable lead-interaction timeline backed by the ``lead_history`` table."""

    async def save(self, event: LeadHistoryEvent) -> None:
        conn = _connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO lead_history
                   (id, workspace_id, lead_id, run_id, event_type, summary, detail, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.id, event.workspace_id, event.lead_id, event.run_id,
                    event.event_type, event.summary, event.detail,
                    _iso(event.created_at),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def list_by_lead(
        self, workspace_id: str, lead_id: str, limit: int = 50
    ) -> list[LeadHistoryEvent]:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT * FROM lead_history WHERE workspace_id = ? AND lead_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (workspace_id, lead_id, limit),
            ).fetchall()
            return [self._row_to_model(r) for r in rows]
        finally:
            conn.close()

    async def list_recent(
        self, workspace_id: str, limit: int = 50, offset: int = 0
    ) -> tuple[list[LeadHistoryEvent], int]:
        conn = _connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM lead_history WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()[0]
            rows = conn.execute(
                """SELECT * FROM lead_history WHERE workspace_id = ?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (workspace_id, limit, offset),
            ).fetchall()
            return [self._row_to_model(r) for r in rows], total
        finally:
            conn.close()

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> LeadHistoryEvent:
        return LeadHistoryEvent(
            id=row["id"],
            workspace_id=row["workspace_id"],
            lead_id=row["lead_id"],
            run_id=row["run_id"] if row["run_id"] else None,
            event_type=row["event_type"],
            summary=row["summary"],
            detail=row["detail"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else utcnow(),
        )


# ---------------------------------------------------------------------------
# SQLiteContactPolicyStore — durable suppression list
# ---------------------------------------------------------------------------


class SQLiteContactPolicyStore:
    """Durable contact-policy (suppression) store backed by ``contact_policy``.

    The LeadIntakeService checks ``is_suppressed`` before creating a lead.
    Suppressed emails are NEVER sent, even with human approval (Correction 7).
    """

    def is_suppressed(self, workspace_id: str, email: str) -> bool:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM contact_policy WHERE workspace_id = ? AND email = ?",
                (workspace_id, email.lower()),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def add(self, workspace_id: str, email: str, reason: str = "manual") -> None:
        conn = _connect()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO contact_policy
                   (workspace_id, email, reason, created_at)
                   VALUES (?, ?, ?, ?)""",
                (workspace_id, email.lower(), reason, _iso(utcnow())),
            )
            conn.commit()
        finally:
            conn.close()

    def remove(self, workspace_id: str, email: str) -> bool:
        conn = _connect()
        try:
            cur = conn.execute(
                "DELETE FROM contact_policy WHERE workspace_id = ? AND email = ?",
                (workspace_id, email.lower()),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def list_for_workspace(self, workspace_id: str) -> list[dict]:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT email, reason, created_at FROM contact_policy WHERE workspace_id = ? ORDER BY created_at DESC",
                (workspace_id,),
            ).fetchall()
            return [
                {"email": r["email"], "reason": r["reason"], "created_at": r["created_at"]}
                for r in rows
            ]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# SQLiteAuditLogger — durable audit trail
# ---------------------------------------------------------------------------


class SQLiteAuditLogger:
    """Durable audit logger backed by the ``audit_events`` table.

    Implements the AuditLogger protocol (log + entries). Records every tool
    call attempt — successful or rejected — with redacted args.
    """

    def log(
        self,
        workspace_id: str,
        run_id: str,
        tool_name: str,
        actor: str,
        args: dict,
        outcome: str,
        rejection_reason: Optional[str] = None,
    ) -> None:
        from opsmesh.security_context import _redact_for_log
        try:
            conn = _connect()
            try:
                conn.execute(
                    """INSERT INTO audit_events
                       (workspace_id, run_id, kind, actor, detail, span_id, occurred_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        workspace_id, run_id or "", tool_name, actor,
                        json.dumps({
                            "args": _redact_for_log(args),
                            "outcome": outcome,
                            "rejection_reason": rejection_reason,
                        }),
                        "", _iso(utcnow()),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            # Audit logging must NEVER break the tool call. Swallow errors.
            pass

    def entries(self) -> list[dict]:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT workspace_id, run_id, kind, actor, detail, occurred_at FROM audit_events ORDER BY occurred_at DESC LIMIT 200"
            ).fetchall()
            return [
                {
                    "workspace_id": r["workspace_id"],
                    "run_id": r["run_id"],
                    "tool_name": r["kind"],
                    "actor": r["actor"],
                    "args": json.loads(r["detail"]) if r["detail"] else {},
                    "outcome": (json.loads(r["detail"]) or {}).get("outcome", ""),
                    "rejection_reason": (json.loads(r["detail"]) or {}).get("rejection_reason"),
                    "timestamp": r["occurred_at"],
                }
                for r in rows
            ]
        except Exception:
            return []


__all__ = [
    "SQLiteSentEmailStore",
    "SQLiteLeadHistoryStore",
    "SQLiteContactPolicyStore",
    "SQLiteAuditLogger",
]
