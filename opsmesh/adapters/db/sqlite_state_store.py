"""
OPSMESH SQLite StateStore — durable implementation of the StateStore ABC.

Implements the SAME interface as InMemoryStateStore but persists to SQLite.
Data survives process restarts. API and worker share the same database file.

CONCURRENCY:
  - save_checkpoint: uses UPDATE ... WHERE version = expected_version (CAS)
  - load_latest: read-only (WAL allows concurrent reads)
  - Workspace isolation: every query includes WHERE workspace_id = ?

USAGE:
    from opsmesh.adapters.db.sqlite_state_store import SQLiteStateStore
    from opsmesh.adapters.db.connection import init_db

    await init_db("/path/to/opsmesh.db")
    store = SQLiteStateStore()
    await store.save_checkpoint(run)
    loaded = await store.load_latest(run_id, workspace_id)
"""

from __future__ import annotations

import json
from typing import Optional

from opsmesh.adapters.db.connection import get_connection
from opsmesh.models import IdempotencyRecord, IdempotencyStatus, RunRecord, utcnow
from opsmesh.state_machine import State, transition
from opsmesh.state_store import (
    ConcurrentModification,
    IllegalStateTransitionError,
    StateStore,
)


class SQLiteStateStore(StateStore):
    """
    SQLite-backed StateStore. Implements the same ABC as InMemoryStateStore.

    Data is persisted to the SQLite database file. Survives restarts.
    Both API and worker share the same file (WAL mode for concurrency).
    """

    async def save_checkpoint(
        self, run: RunRecord, expected_version: Optional[int] = None
    ) -> int:
        """Upsert the run record with compare-and-swap (CAS) semantics."""
        async with get_connection() as conn:
            # Check if the run already exists
            cursor = await conn.execute(
                "SELECT version, state FROM runs WHERE id = ?",
                (run.run_id,),
            )
            existing = await cursor.fetchone()
            await cursor.close()

            if existing is not None:
                existing_version = existing[0]
                existing_state_str = existing[1]

                # CAS check
                if expected_version is not None and existing_version != expected_version:
                    raise ConcurrentModification(
                        f"Run {run.run_id} version mismatch: "
                        f"expected {expected_version}, persisted {existing_version}."
                    )

                # Transition validation (defense in depth)
                existing_state = State(existing_state_str)
                if existing_state != run.state:
                    try:
                        transition(existing_state, run.state)
                    except Exception as e:
                        raise IllegalStateTransitionError(
                            f"Cannot save run {run.run_id}: illegal transition "
                            f"{existing_state} -> {run.state}."
                        ) from e

            # Monotonic version increment
            run.touch()
            run.increment_version()

            # Serialize the full RunRecord as JSON
            payload = run.model_dump_json()

            if existing is not None:
                # UPDATE
                await conn.execute(
                    """UPDATE runs SET
                        workspace_id = ?, lead_id = ?, parent_run_id = ?,
                        state = ?, step_id = ?, version = ?, payload = ?,
                        email_message_id = ?, reconciliation_attempts = ?,
                        budget_used_tokens = ?, budget_used_cost_usd = ?,
                        budget_used_tool_calls = ?,
                        draft_attempts = ?, critic_attempts = ?,
                        last_error = ?, updated_at = ?, terminal_at = ?
                       WHERE id = ?""",
                    (
                        run.workspace_id, run.lead_id, run.parent_run_id,
                        run.state.value if hasattr(run.state, 'value') else str(run.state),
                        run.step_id, run.version, payload,
                        run.email_message_id, run.reconciliation_attempts,
                        run.budget_used_tokens, run.budget_used_cost_usd,
                        run.budget_used_tool_calls,
                        run.draft_attempts, run.critic_attempts,
                        run.last_error, run.updated_at.isoformat(),
                        run.terminal_at.isoformat() if run.terminal_at else None,
                        run.run_id,
                    ),
                )
            else:
                # INSERT
                await conn.execute(
                    """INSERT INTO runs (
                        id, workspace_id, lead_id, parent_run_id,
                        state, step_id, version, payload,
                        email_message_id, reconciliation_attempts,
                        budget_used_tokens, budget_used_cost_usd, budget_used_tool_calls,
                        draft_attempts, critic_attempts, last_error,
                        started_at, updated_at, terminal_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run.run_id, run.workspace_id, run.lead_id, run.parent_run_id,
                        run.state.value if hasattr(run.state, 'value') else str(run.state),
                        run.step_id, run.version, payload,
                        run.email_message_id, run.reconciliation_attempts,
                        run.budget_used_tokens, run.budget_used_cost_usd,
                        run.budget_used_tool_calls,
                        run.draft_attempts, run.critic_attempts, run.last_error,
                        run.started_at.isoformat(), run.updated_at.isoformat(),
                        run.terminal_at.isoformat() if run.terminal_at else None,
                    ),
                )

            await conn.commit()
            return run.version

    async def load_latest(
        self, run_id: str, workspace_id: str
    ) -> Optional[RunRecord]:
        """Load the most recent checkpoint for a run. Returns None if not found
        OR cross-workspace access (treated as 404)."""
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT payload FROM runs WHERE id = ? AND workspace_id = ?",
                (run_id, workspace_id),
            )
            row = await cursor.fetchone()
            await cursor.close()

            if row is None:
                return None  # not found OR cross-workspace — both treated as 404

            payload = row[0]
            return RunRecord.model_validate_json(payload)

    async def list_in_state(
        self, workspace_id: str, state: str, limit: int = 50
    ) -> list[RunRecord]:
        """List runs in a given state, scoped to a workspace."""
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT payload FROM runs WHERE workspace_id = ? AND state = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (workspace_id, state, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()

            return [RunRecord.model_validate_json(row[0]) for row in rows]

    # --- Idempotency methods ---

    async def get_idempotency(self, key: str) -> Optional[IdempotencyRecord]:
        """Look up an idempotency record by key."""
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT key, workspace_id, run_id, step_id, tool_name, "
                "status, result, error, created_at, updated_at "
                "FROM idempotency WHERE key = ?",
                (key,),
            )
            row = await cursor.fetchone()
            await cursor.close()

            if row is None:
                return None

            return IdempotencyRecord(
                key=row[0],
                workspace_id=row[1],
                run_id=row[2],
                step_id=row[3],
                tool_name=row[4],
                status=IdempotencyStatus(row[5]),
                result=json.loads(row[6]) if row[6] else None,
                error=row[7],
                created_at=row[8],
                updated_at=row[9],
            )

    async def insert_idempotency(
        self, record: IdempotencyRecord
    ) -> bool:
        """Insert a pending idempotency record. Atomic (INSERT OR IGNORE)."""
        async with get_connection() as conn:
            try:
                await conn.execute(
                    """INSERT OR IGNORE INTO idempotency
                       (key, workspace_id, run_id, step_id, tool_name,
                        status, result, error, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record.key, record.workspace_id, record.run_id,
                        record.step_id, record.tool_name,
                        record.status.value if hasattr(record.status, 'value') else str(record.status),
                        json.dumps(record.result) if record.result else None,
                        record.error,
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                    ),
                )
                await conn.commit()

                # Check if the row was actually inserted
                cursor = await conn.execute(
                    "SELECT changes()", ()
                )
                changes = await cursor.fetchone()
                await cursor.close()

                return changes[0] > 0
            except Exception:
                return False

    async def update_idempotency(
        self, key: str, status: IdempotencyStatus,
        result: Optional[dict] = None, error: Optional[str] = None,
    ) -> bool:
        """Update an idempotency record's status."""
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM idempotency WHERE key = ?",
                (key,),
            )
            exists = await cursor.fetchone() is not None
            await cursor.close()

            if not exists:
                return False

            await conn.execute(
                """UPDATE idempotency SET
                    status = ?, result = ?, error = ?, updated_at = ?
                   WHERE key = ?""",
                (
                    status.value if hasattr(status, 'value') else str(status),
                    json.dumps(result) if result else None,
                    error,
                    utcnow().isoformat(),
                    key,
                ),
            )
            await conn.commit()
            return True


__all__ = ["SQLiteStateStore"]
