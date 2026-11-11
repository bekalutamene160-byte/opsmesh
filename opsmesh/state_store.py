"""
OPSMESH StateStore — the persistence layer.

Defines an abstract base class (ABC) `StateStore` and one Day 1
implementation: `InMemoryStateStore` (for tests). SQLite and Postgres
implementations arrive in Day 2.

ARCHITECTURE CORRECTIONS APPLIED:
  - Correction 8: Workspace isolation at every layer. load_latest and
                 list_in_state are workspace-scoped; cross-workspace access
                 returns None / empty list.
  - Correction 14: Monotonic versioning with compare-and-swap (CAS).
                   save_checkpoint takes expected_version; raises
                   ConcurrentModification on mismatch.
  - Correction 15: ABC, not Protocol. Documented as ABC.
  - Correction 19: IdempotencyRecord typed model, not dict.

The supervisor depends on the ABC, not a concrete class. We can swap
InMemory ↔ SQLite ↔ Postgres without touching business logic. This is
dependency inversion: the supervisor doesn't know or care where state lives.

NOTE: The in-memory implementation does NOT prove production database
concurrency. It is for tests and local development only. The Postgres
implementation (Day 2) will use UPDATE ... WHERE version = $expected for
true atomic CAS.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from opsmesh.models import IdempotencyRecord, IdempotencyStatus, RunRecord, utcnow
from opsmesh.state_machine import State, transition


class StateStoreError(Exception):
    """Base class for StateStore errors."""


class ConcurrentModification(StateStoreError):
    """Another worker advanced the run's version past ours between load
    and save. The caller should reload and retry."""


class IllegalStateTransitionError(StateStoreError):
    """The run's state transition is not in ALLOWED_TRANSITIONS. The
    StateStore rejects the write to prevent illegal state from reaching
    disk."""


class StateStore(ABC):
    """
    Abstract base class for the persistence layer.

    Implementations:
        - InMemoryStateStore  (Day 1 — tests and local dev)
        - SQLiteStateStore    (Day 2 — local dev with persistence)
        - PostgresStateStore  (Day 2 — production)

    The supervisor depends on this ABC, not on any concrete implementation.
    """

    # --- Checkpoint / load ---

    @abstractmethod
    async def save_checkpoint(
        self, run: RunRecord, expected_version: Optional[int] = None
    ) -> int:
        """
        Upsert the run record with compare-and-swap (CAS) semantics.

        Args:
            run: the run record to save.
            expected_version: if provided, the write only succeeds if the
                persisted version matches. If None, no version check (used
                for the initial save of a NEW run).

        Returns:
            The new version number after the save.

        Raises:
            ConcurrentModification: if expected_version doesn't match the
                persisted version.
            IllegalStateTransitionError: if the run's state transition from
                the persisted state to the new state is not legal.
        """

    @abstractmethod
    async def load_latest(
        self, run_id: str, workspace_id: str
    ) -> Optional[RunRecord]:
        """
        Load the most recent checkpoint for a run.

        Returns None if:
            - The run does not exist.
            - The run belongs to a different workspace (cross-workspace
              access attempt — silently returns None; caller treats as 404).

        This is the workspace isolation enforcement point. (Correction 8.)
        """

    @abstractmethod
    async def list_in_state(
        self, workspace_id: str, state: str, limit: int = 50
    ) -> list[RunRecord]:
        """List runs in a given state, scoped to a workspace.
        Used by the dashboard (GET /api/v1/runs?state=awaiting_approval)."""

    # --- Idempotency (used by IdempotentToolExecutor on Day 3) ---

    @abstractmethod
    async def get_idempotency(self, key: str) -> Optional[IdempotencyRecord]:
        """Look up an idempotency record by key. None if not found."""

    @abstractmethod
    async def insert_idempotency(
        self, record: IdempotencyRecord
    ) -> bool:
        """
        Insert a pending idempotency record. Atomic.

        Returns True if inserted, False if the key already exists
        (race-condition resolution — caller re-reads on False).
        """

    @abstractmethod
    async def update_idempotency(
        self,
        key: str,
        status: IdempotencyStatus,
        result: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> bool:
        """
        Update an idempotency record's status.

        Returns True if the record existed and was updated.
        Returns False if the record does not exist (caller should handle
        this — it indicates a bug: update without prior insert).
        """


# ---------------------------------------------------------------------------
# InMemoryStateStore — for unit tests. Fast, no I/O, not durable.
# ---------------------------------------------------------------------------


class InMemoryStateStore(StateStore):
    """
    In-memory implementation. Lives in process memory; lost on restart.

    USE ONLY FOR: unit tests, local development, and the reproducible demo.
    DO NOT USE IN PRODUCTION. This implementation does not prove production
    database concurrency — the Postgres implementation (Day 2) uses
    UPDATE ... WHERE version = $expected for true atomic CAS.

    WHY DEEP COPIES?
        Without them, the caller could mutate the in-memory state after
        saving, breaking the "what's in the DB is what's persisted"
        invariant. SQLite/Postgres naturally enforce this (rows are
        immutable once written); the in-memory store has to fake it.

    CONCURRENCY MODEL:
        asyncio is single-threaded by default (no true parallelism within
        one event loop), so plain dicts are safe here. For multi-process
        tests, use SQLite (Day 2).
    """

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._idempotency: dict[str, IdempotencyRecord] = {}

    async def save_checkpoint(
        self, run: RunRecord, expected_version: Optional[int] = None
    ) -> int:
        existing = self._runs.get(run.run_id)

        # --- CAS check (Correction 14) ---
        if existing is not None and expected_version is not None:
            if existing.version != expected_version:
                raise ConcurrentModification(
                    f"Run {run.run_id} version mismatch: "
                    f"expected {expected_version}, persisted {existing.version}. "
                    f"Another worker advanced the state."
                )

        # --- Transition validation (defense in depth) ---
        if existing is not None:
            # Same-state saves are allowed (e.g., updating step_id or version
            # without changing state). This is NOT a transition.
            # State inherits from str, so == works for both State and str.
            if existing.state != run.state:
                try:
                    transition(existing.state, run.state)
                except Exception as e:
                    raise IllegalStateTransitionError(
                        f"Cannot save run {run.run_id}: illegal transition "
                        f"{existing.state} -> {run.state}."
                    ) from e

        # --- Monotonic version increment ---
        run.touch()
        run.increment_version()

        # --- Defensive copy ---
        self._runs[run.run_id] = run.model_copy(deep=True)
        return run.version

    async def load_latest(
        self, run_id: str, workspace_id: str
    ) -> Optional[RunRecord]:
        run = self._runs.get(run_id)
        if run is None or run.workspace_id != workspace_id:
            # Not found OR cross-workspace — both treated as 404.
            return None
        return run.model_copy(deep=True)

    async def list_in_state(
        self, workspace_id: str, state: str, limit: int = 50
    ) -> list[RunRecord]:
        # State inherits from str, so State.RESEARCHING == "researching" is True.
        # This works for both State enum instances and plain strings.
        return [
            r.model_copy(deep=True)
            for r in self._runs.values()
            if r.workspace_id == workspace_id and r.state == state
        ][:limit]

    async def get_idempotency(self, key: str) -> Optional[IdempotencyRecord]:
        record = self._idempotency.get(key)
        return record.model_copy(deep=True) if record else None

    async def insert_idempotency(
        self, record: IdempotencyRecord
    ) -> bool:
        if record.key in self._idempotency:
            return False  # race: another worker won
        self._idempotency[record.key] = record.model_copy(deep=True)
        return True

    async def update_idempotency(
        self,
        key: str,
        status: IdempotencyStatus,
        result: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> bool:
        record = self._idempotency.get(key)
        if record is None:
            return False  # record doesn't exist — caller bug
        record.status = status
        if result is not None:
            record.result = result
        if error is not None:
            record.error = error
        record.updated_at = utcnow()
        self._idempotency[key] = record.model_copy(deep=True)
        return True


__all__ = [
    "StateStore",
    "StateStoreError",
    "ConcurrentModification",
    "IllegalStateTransitionError",
    "InMemoryStateStore",
]
