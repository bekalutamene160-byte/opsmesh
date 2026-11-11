"""
OPSMESH SQLite CRMOutboxStore — durable CRM outbox operations.

Implements the same interface as the in-memory CRMOutboxStore but persists
to SQLite. Outbox operations survive process restarts.

CONCURRENCY:
  claim_pending(): BEGIN IMMEDIATE TRANSACTION
                     UPDATE crm_outbox SET status='in_progress', attempts=attempts+1
                     WHERE id IN (
                         SELECT id FROM crm_outbox
                         WHERE status='pending' AND next_attempt_at <= ?
                         ORDER BY next_attempt_at LIMIT ?
                     )
                   COMMIT

USAGE:
    from opsmesh.adapters.db.sqlite_outbox_store import SQLiteOutboxStore
    store = SQLiteOutboxStore()
    store.enqueue(op)
    pending = store.claim_pending(limit=10)
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from opsmesh.adapters.db.connection import get_connection, get_db_path
from opsmesh.models import CRMOutboxOp, CRMOutboxStatus, utcnow


class SQLiteOutboxStore:
    """
    SQLite-backed CRM outbox store. Durable — operations survive restarts.

    Implements the same interface as the in-memory CRMOutboxStore.
    """

    def enqueue(self, op: CRMOutboxOp) -> None:
        """Add a pending outbox operation."""
        import sqlite3
        db_path = get_db_path()
        if db_path is None:
            raise RuntimeError("Database not initialized")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        conn.execute(
            """INSERT INTO crm_outbox
               (id, workspace_id, run_id, lead_id, operation, args,
                status, attempts, max_attempts, next_attempt_at,
                last_error, created_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                op.id, op.workspace_id, op.run_id, op.lead_id,
                op.operation.value if hasattr(op.operation, 'value') else str(op.operation),
                json.dumps(op.args),
                op.status.value if hasattr(op.status, 'value') else str(op.status),
                op.attempts, op.max_attempts,
                op.next_attempt_at.isoformat() if hasattr(op.next_attempt_at, 'isoformat') else str(op.next_attempt_at),
                op.last_error,
                op.created_at.isoformat(),
                op.completed_at.isoformat() if op.completed_at else None,
            ),
        )
        conn.commit()
        conn.close()

    def get(self, op_id: str) -> Optional[CRMOutboxOp]:
        """Fetch an outbox operation by ID."""
        import sqlite3
        db_path = get_db_path()
        if db_path is None:
            raise RuntimeError("Database not initialized")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute(
            "SELECT id, workspace_id, run_id, lead_id, operation, args, "
            "status, attempts, max_attempts, next_attempt_at, last_error, "
            "created_at, completed_at FROM crm_outbox WHERE id = ?",
            (op_id,),
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row is None:
            return None
        return self._row_to_op(row)

    async def claim_pending(self, limit: int = 10) -> list[CRMOutboxOp]:
        """Claim pending ops atomically using BEGIN IMMEDIATE."""
        now = utcnow().isoformat()

        async with get_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE TRANSACTION")

            try:
                # Select pending ops whose next_attempt_at has passed
                cursor = await conn.execute(
                    "SELECT id FROM crm_outbox "
                    "WHERE status = 'pending' AND next_attempt_at <= ? "
                    "ORDER BY next_attempt_at LIMIT ?",
                    (now, limit),
                )
                ids = [row[0] for row in await cursor.fetchall()]
                await cursor.close()

                if not ids:
                    await conn.execute("COMMIT")
                    return []

                # Mark them as in_progress + increment attempts
                placeholders = ",".join("?" * len(ids))
                await conn.execute(
                    f"UPDATE crm_outbox SET status = 'in_progress', "
                    f"attempts = attempts + 1 WHERE id IN ({placeholders})",
                    ids,
                )
                await conn.execute("COMMIT")

                # Fetch the claimed ops
                cursor = await conn.execute(
                    f"SELECT id, workspace_id, run_id, lead_id, operation, args, "
                    f"status, attempts, max_attempts, next_attempt_at, last_error, "
                    f"created_at, completed_at FROM crm_outbox WHERE id IN ({placeholders})",
                    ids,
                )
                rows = await cursor.fetchall()
                await cursor.close()

                return [self._row_to_op(row) for row in rows]

            except Exception:
                await conn.execute("ROLLBACK")
                raise

    def mark_done(self, op_id: str) -> None:
        """Mark an outbox operation as done."""
        import sqlite3
        db_path = get_db_path()
        if db_path is None:
            raise RuntimeError("Database not initialized")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "UPDATE crm_outbox SET status = 'done', completed_at = ? WHERE id = ?",
            (utcnow().isoformat(), op_id),
        )
        conn.commit()
        conn.close()

    def mark_failed(self, op_id: str, error: str) -> None:
        """Mark an outbox operation as permanently failed."""
        import sqlite3
        db_path = get_db_path()
        if db_path is None:
            raise RuntimeError("Database not initialized")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "UPDATE crm_outbox SET status = 'failed', last_error = ?, completed_at = ? WHERE id = ?",
            (error, utcnow().isoformat(), op_id),
        )
        conn.commit()
        conn.close()

    def mark_dead_letter(self, op_id: str, error: str) -> None:
        """Move an outbox operation to the dead-letter queue."""
        import sqlite3
        db_path = get_db_path()
        if db_path is None:
            raise RuntimeError("Database not initialized")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "UPDATE crm_outbox SET status = 'dead_letter', last_error = ?, completed_at = ? WHERE id = ?",
            (error, utcnow().isoformat(), op_id),
        )
        conn.commit()
        conn.close()

    def requeue(self, op_id: str, error: str) -> None:
        """Put a transiently-failed op back to PENDING with backoff."""
        import sqlite3
        from opsmesh.reconciler.crm_outbox import BACKOFF_SCHEDULE

        db_path = get_db_path()
        if db_path is None:
            raise RuntimeError("Database not initialized")

        # Get current attempts to calculate backoff
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute(
            "SELECT attempts FROM crm_outbox WHERE id = ?",
            (op_id,),
        )
        row = cursor.fetchone()
        cursor.close()

        attempts = row[0] if row else 1
        idx = min(attempts - 1, len(BACKOFF_SCHEDULE) - 1)
        delay = BACKOFF_SCHEDULE[idx]
        from datetime import timedelta
        next_attempt = utcnow() + timedelta(seconds=delay)

        conn.execute(
            "UPDATE crm_outbox SET status = 'pending', last_error = ?, next_attempt_at = ? WHERE id = ?",
            (error, next_attempt.isoformat(), op_id),
        )
        conn.commit()
        conn.close()

    def list_pending(self) -> list[CRMOutboxOp]:
        """List all pending outbox operations."""
        import sqlite3
        db_path = get_db_path()
        if db_path is None:
            raise RuntimeError("Database not initialized")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute(
            "SELECT id, workspace_id, run_id, lead_id, operation, args, "
            "status, attempts, max_attempts, next_attempt_at, last_error, "
            "created_at, completed_at FROM crm_outbox WHERE status = 'pending'"
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return [self._row_to_op(row) for row in rows]

    def list_done(self) -> list[CRMOutboxOp]:
        """List all completed outbox operations."""
        import sqlite3
        db_path = get_db_path()
        if db_path is None:
            raise RuntimeError("Database not initialized")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute(
            "SELECT id, workspace_id, run_id, lead_id, operation, args, "
            "status, attempts, max_attempts, next_attempt_at, last_error, "
            "created_at, completed_at FROM crm_outbox WHERE status = 'done'"
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return [self._row_to_op(row) for row in rows]

    def list_dead_letter(self) -> list[CRMOutboxOp]:
        """List all dead-lettered outbox operations."""
        import sqlite3
        db_path = get_db_path()
        if db_path is None:
            raise RuntimeError("Database not initialized")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute(
            "SELECT id, workspace_id, run_id, lead_id, operation, args, "
            "status, attempts, max_attempts, next_attempt_at, last_error, "
            "created_at, completed_at FROM crm_outbox WHERE status = 'dead_letter'"
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return [self._row_to_op(row) for row in rows]

    def _row_to_op(self, row) -> CRMOutboxOp:
        """Convert a database row to a CRMOutboxOp."""
        from opsmesh.models import CRMOutboxOperation

        def parse_dt(s):
            if s is None:
                return None
            return datetime.fromisoformat(s)

        return CRMOutboxOp(
            id=row[0], workspace_id=row[1], run_id=row[2], lead_id=row[3],
            operation=CRMOutboxOperation(row[4]),
            args=json.loads(row[5]) if row[5] else {},
            status=CRMOutboxStatus(row[6]),
            attempts=row[7], max_attempts=row[8],
            next_attempt_at=parse_dt(row[9]) or utcnow(),
            last_error=row[10],
            created_at=parse_dt(row[11]) or utcnow(),
            completed_at=parse_dt(row[12]),
        )


__all__ = ["SQLiteOutboxStore"]
