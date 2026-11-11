"""
OPSMESH SQLite ApprovalStore — durable approval verification.

Implements the ApprovalStore Protocol using SQLite. Approval decisions are
atomic via BEGIN IMMEDIATE + UPDATE WHERE status='pending'.

CONCURRENCY:
  decide(): BEGIN IMMEDIATE TRANSACTION
              UPDATE approvals SET status=?, decided_by=?, ...
              WHERE id=? AND workspace_id=? AND status='pending'
            COMMIT
            → If 0 rows updated → AlreadyResolvedError (someone else won)

USAGE:
    from opsmesh.adapters.db.sqlite_approval_store import SQLiteApprovalStore
    store = SQLiteApprovalStore()
    record = store.create(workspace_id="ws_1", run_id="run_1", ...)
    await store.decide(approval_id, workspace_id="ws_1", new_status=ApprovalStatus.APPROVED, ...)
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Optional

from opsmesh.adapters.db.connection import get_connection
from opsmesh.approval_store import (
    DEFAULT_APPROVAL_TIMEOUT,
    ApprovalConsumedError,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
)
from opsmesh.models import (
    ApprovalKind,
    ApprovalRecord,
    ApprovalStatus,
    utcnow,
)


class SQLiteApprovalStore:
    """
    SQLite-backed ApprovalStore. Approval decisions are durable + atomic.

    Implements the same interface as InMemoryApprovalStore.
    Data survives process restarts.
    """

    def create(
        self,
        workspace_id: str,
        run_id: str,
        kind: ApprovalKind,
        action: str,
        payload: dict,
        payload_hash: str,
        expires_at=None,
    ) -> ApprovalRecord:
        """Create a new pending approval record."""
        if expires_at is None:
            expires_at = utcnow() + DEFAULT_APPROVAL_TIMEOUT

        record = ApprovalRecord(
            workspace_id=workspace_id,
            run_id=run_id,
            kind=kind,
            action=action,
            payload=payload,
            payload_hash=payload_hash,
            expires_at=expires_at,
        )

        # Synchronous insert (called from async context via approval_gate)
        import sqlite3
        from opsmesh.adapters.db.connection import get_db_path
        db_path = get_db_path()
        if db_path is None:
            raise RuntimeError("Database not initialized")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        conn.execute(
            """INSERT INTO approvals
               (id, workspace_id, run_id, kind, status, action, payload,
                payload_hash, decision, decision_feedback, decided_by,
                decided_at, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.id, record.workspace_id, record.run_id,
                record.kind.value if hasattr(record.kind, 'value') else str(record.kind),
                record.status.value if hasattr(record.status, 'value') else str(record.status),
                record.action, json.dumps(record.payload),
                record.payload_hash, record.decision,
                record.decision_feedback, record.decided_by,
                record.decided_at.isoformat() if record.decided_at else None,
                record.created_at.isoformat(),
                record.expires_at.isoformat() if hasattr(record.expires_at, 'isoformat') else str(record.expires_at),
            ),
        )
        conn.commit()
        conn.close()
        return record

    def get(self, approval_id: str) -> Optional[ApprovalRecord]:
        """Fetch an approval by ID."""
        import sqlite3
        from opsmesh.adapters.db.connection import get_db_path
        db_path = get_db_path()
        if db_path is None:
            raise RuntimeError("Database not initialized")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute(
            "SELECT id, workspace_id, run_id, kind, status, action, payload, "
            "payload_hash, decision, decision_feedback, decided_by, decided_at, "
            "created_at, expires_at FROM approvals WHERE id = ?",
            (approval_id,),
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row is None:
            return None

        from datetime import datetime
        def parse_dt(s):
            if s is None:
                return None
            return datetime.fromisoformat(s)

        return ApprovalRecord(
            id=row[0], workspace_id=row[1], run_id=row[2],
            kind=ApprovalKind(row[3]),
            status=ApprovalStatus(row[4]),
            action=row[5], payload=json.loads(row[6]),
            payload_hash=row[7], decision=row[8],
            decision_feedback=row[9], decided_by=row[10],
            decided_at=parse_dt(row[11]),
            created_at=parse_dt(row[12]),
            expires_at=parse_dt(row[13]),
        )

    async def decide(
        self,
        approval_id: str,
        workspace_id: str,
        new_status: ApprovalStatus,
        decision: str,
        decided_by: str,
        feedback: Optional[str] = None,
    ) -> ApprovalRecord:
        """Atomically transition PENDING → decided status.

        Uses BEGIN IMMEDIATE + UPDATE WHERE status='pending' for atomic CAS.
        If 0 rows updated → AlreadyResolvedError (another caller won).
        """
        from opsmesh.approval_gate import AlreadyResolvedError

        async with get_connection() as conn:
            # BEGIN IMMEDIATE — acquire write lock
            await conn.execute("BEGIN IMMEDIATE TRANSACTION")

            try:
                # Check existence
                cursor = await conn.execute(
                    "SELECT id, workspace_id, status FROM approvals WHERE id = ?",
                    (approval_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()

                if row is None:
                    await conn.execute("ROLLBACK")
                    raise ApprovalNotFoundError(approval_id)

                if row[1] != workspace_id:
                    await conn.execute("ROLLBACK")
                    raise ApprovalError(
                        "workspace_mismatch",
                        f"Approval {approval_id} belongs to workspace {row[1]}, "
                        f"not {workspace_id}",
                    )

                current_status = row[2]
                if current_status != "pending":
                    await conn.execute("ROLLBACK")
                    raise AlreadyResolvedError(approval_id, current_status)

                # Atomic CAS: UPDATE WHERE status='pending'
                cursor = await conn.execute(
                    """UPDATE approvals SET
                        status = ?, decision = ?, decision_feedback = ?,
                        decided_by = ?, decided_at = ?
                       WHERE id = ? AND workspace_id = ? AND status = 'pending'""",
                    (
                        new_status.value if hasattr(new_status, 'value') else str(new_status),
                        decision, feedback, decided_by,
                        utcnow().isoformat(),
                        approval_id, workspace_id,
                    ),
                )
                changes = cursor.rowcount
                await cursor.close()

                if changes == 0:
                    await conn.execute("ROLLBACK")
                    raise AlreadyResolvedError(approval_id, "pending")

                await conn.execute("COMMIT")

            except (ApprovalNotFoundError, ApprovalError, AlreadyResolvedError):
                raise
            except Exception:
                await conn.execute("ROLLBACK")
                raise

        # Return the updated record
        return self.get(approval_id)

    async def verify_and_consume(
        self,
        approval_id: str,
        workspace_id: str,
        run_id: str,
        action: str,
        payload_hash: str,
    ) -> ApprovalRecord:
        """Atomically verify + consume an approval."""
        async with get_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE TRANSACTION")

            try:
                cursor = await conn.execute(
                    "SELECT id, workspace_id, run_id, kind, status, action, "
                    "payload, payload_hash, decision, decision_feedback, "
                    "decided_by, decided_at, created_at, expires_at "
                    "FROM approvals WHERE id = ?",
                    (approval_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()

                if row is None:
                    await conn.execute("ROLLBACK")
                    raise ApprovalNotFoundError(approval_id)

                record = ApprovalRecord(
                    id=row[0], workspace_id=row[1], run_id=row[2],
                    kind=ApprovalKind(row[3]),
                    status=ApprovalStatus(row[4]),
                    action=row[5], payload=json.loads(row[6]) if row[6] else {},
                    payload_hash=row[7], decision=row[8],
                    decision_feedback=row[9], decided_by=row[10],
                    decided_at=row[11],
                    created_at=row[12],
                    expires_at=row[13],
                )

                # Check expiration
                if utcnow() > record.expires_at:
                    await conn.execute(
                        "UPDATE approvals SET status = 'expired' WHERE id = ?",
                        (approval_id,),
                    )
                    await conn.execute("COMMIT")
                    raise ApprovalExpiredError(approval_id)

                if record.status == ApprovalStatus.CONSUMED:
                    await conn.execute("ROLLBACK")
                    raise ApprovalConsumedError(approval_id)

                if record.status != ApprovalStatus.APPROVED:
                    await conn.execute("ROLLBACK")
                    raise ApprovalError(
                        "not_approved",
                        f"Approval {approval_id} status is {record.status.value}, "
                        f"not APPROVED — cannot be consumed",
                    )

                if record.workspace_id != workspace_id:
                    await conn.execute("ROLLBACK")
                    raise ApprovalError(
                        "workspace_mismatch",
                        f"Approval belongs to {record.workspace_id}, not {workspace_id}",
                    )

                if record.run_id != run_id:
                    await conn.execute("ROLLBACK")
                    raise ApprovalError(
                        "run_mismatch",
                        f"Approval belongs to run {record.run_id}, not {run_id}",
                    )

                if record.action != action:
                    await conn.execute("ROLLBACK")
                    raise ApprovalError(
                        "action_mismatch",
                        f"Approval authorizes '{record.action}', not '{action}'",
                    )

                if record.payload_hash != payload_hash:
                    await conn.execute("ROLLBACK")
                    raise ApprovalError(
                        "payload_mismatch",
                        f"Payload hash mismatch",
                    )

                # All checks passed — consume
                await conn.execute(
                    "UPDATE approvals SET status = 'consumed' WHERE id = ?",
                    (approval_id,),
                )
                await conn.execute("COMMIT")

                record.status = ApprovalStatus.CONSUMED
                return record

            except (ApprovalNotFoundError, ApprovalError, ApprovalExpiredError,
                    ApprovalConsumedError):
                raise
            except Exception:
                await conn.execute("ROLLBACK")
                raise

    def find_expired_pending(self) -> list[ApprovalRecord]:
        """Find all pending approvals that have passed their expiration."""
        import sqlite3
        from opsmesh.adapters.db.connection import get_db_path
        db_path = get_db_path()
        if db_path is None:
            raise RuntimeError("Database not initialized")

        now = utcnow().isoformat()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute(
            "SELECT id, workspace_id, run_id, kind, status, action, payload, "
            "payload_hash, decision, decision_feedback, decided_by, decided_at, "
            "created_at, expires_at FROM approvals "
            "WHERE status = 'pending' AND expires_at < ?",
            (now,),
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        from datetime import datetime
        def parse_dt(s):
            if s is None:
                return None
            return datetime.fromisoformat(s)

        return [
            ApprovalRecord(
                id=row[0], workspace_id=row[1], run_id=row[2],
                kind=ApprovalKind(row[3]),
                status=ApprovalStatus(row[4]),
                action=row[5], payload=json.loads(row[6]) if row[6] else {},
                payload_hash=row[7], decision=row[8],
                decision_feedback=row[9], decided_by=row[10],
                decided_at=parse_dt(row[11]),
                created_at=parse_dt(row[12]),
                expires_at=parse_dt(row[13]),
            )
            for row in rows
        ]

    def list_pending(self, workspace_id: str) -> list[ApprovalRecord]:
        """List all pending approvals for a workspace.

        Used by the API's /approvals endpoint to show pending approvals
        in the browser. Without this, the API couldn't see approvals
        created by the worker (they're in the same DB but the API was
        using an in-memory store).
        """
        import sqlite3
        from opsmesh.adapters.db.connection import get_db_path
        db_path = get_db_path()
        if db_path is None:
            raise RuntimeError("Database not initialized")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute(
            "SELECT id, workspace_id, run_id, kind, status, action, payload, "
            "payload_hash, decision, decision_feedback, decided_by, decided_at, "
            "created_at, expires_at FROM approvals "
            "WHERE workspace_id = ? AND status = 'pending' "
            "ORDER BY created_at DESC",
            (workspace_id,),
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        from datetime import datetime
        def parse_dt(s):
            if s is None:
                return None
            return datetime.fromisoformat(s)

        return [
            ApprovalRecord(
                id=row[0], workspace_id=row[1], run_id=row[2],
                kind=ApprovalKind(row[3]),
                status=ApprovalStatus(row[4]),
                action=row[5], payload=json.loads(row[6]) if row[6] else {},
                payload_hash=row[7], decision=row[8],
                decision_feedback=row[9], decided_by=row[10],
                decided_at=parse_dt(row[11]),
                created_at=parse_dt(row[12]),
                expires_at=parse_dt(row[13]),
            )
            for row in rows
        ]


__all__ = ["SQLiteApprovalStore"]
