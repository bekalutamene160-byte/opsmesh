"""
Tests for opsmesh.state_store (InMemoryStateStore).

Covers:
    - Fresh store isolation
    - Workspace-scoped reads
    - Workspace collision rejection
    - Monotonic checkpoint versioning (Correction 14)
    - Stale checkpoint rejection (CAS)
    - Optimistic concurrency
    - Transition validation before write (defense in depth)
    - Defensive-copy behavior
    - Idempotency insert/read/update
    - Missing idempotency record behavior
"""

from __future__ import annotations

import pytest

from opsmesh.models import (
    IdempotencyRecord,
    IdempotencyStatus,
    ResearchResult,
    RunRecord,
)
from opsmesh.state_machine import State
from opsmesh.state_store import (
    ConcurrentModification,
    IllegalStateTransitionError,
    InMemoryStateStore,
)


# ---------------------------------------------------------------------------
# 1. Fresh store isolation
# ---------------------------------------------------------------------------


class TestFreshStoreIsolation:
    def test_new_store_is_empty(self, fresh_store):
        assert fresh_store._runs == {}
        assert fresh_store._idempotency == {}

    @pytest.mark.asyncio
    async def test_each_fixture_gets_new_store(self, fresh_store, sample_run):
        # Save a run, verify it's there
        await fresh_store.save_checkpoint(sample_run)
        loaded = await fresh_store.load_latest(sample_run.run_id, sample_run.workspace_id)
        assert loaded is not None

    @pytest.mark.asyncio
    async def test_different_stores_dont_share_data(self):
        store1 = InMemoryStateStore()
        store2 = InMemoryStateStore()
        run = RunRecord(workspace_id="ws_1", lead_id="lead_1")
        await store1.save_checkpoint(run)
        assert await store2.load_latest(run.run_id, "ws_1") is None


# ---------------------------------------------------------------------------
# 2. Workspace-scoped reads (Correction 8)
# ---------------------------------------------------------------------------


class TestWorkspaceScoping:
    @pytest.mark.asyncio
    async def test_load_latest_returns_none_for_cross_workspace(
        self, fresh_store, sample_workspace_id, other_workspace_id, sample_lead_id
    ):
        """Cross-workspace access returns None (treated as 404)."""
        run = RunRecord(
            workspace_id=sample_workspace_id, lead_id=sample_lead_id,
        )
        await fresh_store.save_checkpoint(run)
        # Try to load from a different workspace
        loaded = await fresh_store.load_latest(run.run_id, other_workspace_id)
        assert loaded is None

    @pytest.mark.asyncio
    async def test_load_latest_returns_run_for_correct_workspace(
        self, fresh_store, sample_workspace_id, sample_lead_id
    ):
        run = RunRecord(workspace_id=sample_workspace_id, lead_id=sample_lead_id)
        await fresh_store.save_checkpoint(run)
        loaded = await fresh_store.load_latest(run.run_id, sample_workspace_id)
        assert loaded is not None
        assert loaded.workspace_id == sample_workspace_id

    @pytest.mark.asyncio
    async def test_list_in_state_is_workspace_scoped(
        self, fresh_store, sample_workspace_id, other_workspace_id
    ):
        # Create runs in two workspaces, both in RESEARCHING state
        run1 = RunRecord(
            workspace_id=sample_workspace_id, lead_id="lead_1",
            state=State.RESEARCHING,
        )
        run2 = RunRecord(
            workspace_id=other_workspace_id, lead_id="lead_2",
            state=State.RESEARCHING,
        )
        await fresh_store.save_checkpoint(run1)
        await fresh_store.save_checkpoint(run2)

        # List from workspace 1 — should only see run1
        runs = await fresh_store.list_in_state(sample_workspace_id, "researching")
        assert len(runs) == 1
        assert runs[0].run_id == run1.run_id

        # List from workspace 2 — should only see run2
        runs = await fresh_store.list_in_state(other_workspace_id, "researching")
        assert len(runs) == 1
        assert runs[0].run_id == run2.run_id

    @pytest.mark.asyncio
    async def test_list_in_state_respects_limit(
        self, fresh_store, sample_workspace_id
    ):
        for i in range(5):
            run = RunRecord(
                workspace_id=sample_workspace_id, lead_id=f"lead_{i}",
                state=State.RESEARCHING,
            )
            await fresh_store.save_checkpoint(run)
        runs = await fresh_store.list_in_state(sample_workspace_id, "researching", limit=3)
        assert len(runs) == 3


# ---------------------------------------------------------------------------
# 3. Monotonic checkpoint versioning (Correction 14)
# ---------------------------------------------------------------------------


class TestMonotonicVersioning:
    @pytest.mark.asyncio
    async def test_version_increments_on_each_save(self, fresh_store, sample_run):
        v1 = await fresh_store.save_checkpoint(sample_run)
        assert v1 == 1
        v2 = await fresh_store.save_checkpoint(sample_run, expected_version=1)
        assert v2 == 2
        v3 = await fresh_store.save_checkpoint(sample_run, expected_version=2)
        assert v3 == 3

    @pytest.mark.asyncio
    async def test_version_starts_at_1_for_new_run(self, fresh_store, sample_run):
        v = await fresh_store.save_checkpoint(sample_run)
        assert v == 1
        assert sample_run.version == 1

    @pytest.mark.asyncio
    async def test_initial_save_without_expected_version(self, fresh_store, sample_run):
        """First save of a new run doesn't need expected_version."""
        v = await fresh_store.save_checkpoint(sample_run, expected_version=None)
        assert v == 1


# ---------------------------------------------------------------------------
# 4. Stale checkpoint rejection (CAS) (Correction 14)
# ---------------------------------------------------------------------------


class TestStaleCheckpointRejection:
    @pytest.mark.asyncio
    async def test_stale_version_raises_concurrent_modification(
        self, fresh_store, sample_run
    ):
        """If the persisted version doesn't match expected_version, raise."""
        await fresh_store.save_checkpoint(sample_run)  # version → 1
        # Simulate another worker advancing the version
        loaded = await fresh_store.load_latest(sample_run.run_id, sample_run.workspace_id)
        assert loaded is not None
        await fresh_store.save_checkpoint(loaded, expected_version=1)  # version → 2

        # Now try to save with the stale expected_version=1
        with pytest.raises(ConcurrentModification):
            await fresh_store.save_checkpoint(sample_run, expected_version=1)

    @pytest.mark.asyncio
    async def test_correct_version_succeeds(self, fresh_store, sample_run):
        await fresh_store.save_checkpoint(sample_run)  # version → 1
        sample_run.state = State.RESEARCHING
        v = await fresh_store.save_checkpoint(sample_run, expected_version=1)
        assert v == 2


# ---------------------------------------------------------------------------
# 5. Optimistic concurrency
# ---------------------------------------------------------------------------


class TestOptimisticConcurrency:
    @pytest.mark.asyncio
    async def test_two_workers_same_run_second_fails(
        self, fresh_store, sample_run
    ):
        """Simulate two workers loading the same run and both trying to save."""
        await fresh_store.save_checkpoint(sample_run)  # version → 1

        # Worker A loads
        run_a = await fresh_store.load_latest(sample_run.run_id, sample_run.workspace_id)
        # Worker B loads (same version)
        run_b = await fresh_store.load_latest(sample_run.run_id, sample_run.workspace_id)

        assert run_a is not None
        assert run_b is not None
        assert run_a.version == run_b.version == 1

        # Worker A saves first — succeeds
        run_a.state = State.RESEARCHING
        await fresh_store.save_checkpoint(run_a, expected_version=1)

        # Worker B tries to save with stale version — fails
        run_b.state = State.RESEARCHING
        with pytest.raises(ConcurrentModification):
            await fresh_store.save_checkpoint(run_b, expected_version=1)


# ---------------------------------------------------------------------------
# 6. Transition validation before write (defense in depth)
# ---------------------------------------------------------------------------


class TestTransitionValidation:
    @pytest.mark.asyncio
    async def test_illegal_transition_rejected(
        self, fresh_store, sample_run
    ):
        """StateStore rejects illegal transitions before writing."""
        await fresh_store.save_checkpoint(sample_run)  # state=NEW, version=1

        # Try to save a run that jumps NEW → SENT (illegal)
        sample_run.state = State.SENT
        with pytest.raises(IllegalStateTransitionError):
            await fresh_store.save_checkpoint(sample_run, expected_version=1)

    @pytest.mark.asyncio
    async def test_legal_transition_accepted(
        self, fresh_store, sample_run
    ):
        await fresh_store.save_checkpoint(sample_run)  # state=NEW, version=1
        sample_run.state = State.RESEARCHING  # legal: NEW → RESEARCHING
        await fresh_store.save_checkpoint(sample_run, expected_version=1)


# ---------------------------------------------------------------------------
# 7. Defensive-copy behavior
# ---------------------------------------------------------------------------


class TestDefensiveCopy:
    @pytest.mark.asyncio
    async def test_save_then_mutate_does_not_affect_persisted(
        self, fresh_store, sample_run
    ):
        """After saving, mutating the caller's run must not affect what's
        stored in the StateStore."""
        await fresh_store.save_checkpoint(sample_run)
        # Mutate the caller's object
        sample_run.state = State.RESEARCHING
        sample_run.step_id = 999

        # Load — should get the original saved state
        loaded = await fresh_store.load_latest(sample_run.run_id, sample_run.workspace_id)
        assert loaded is not None
        assert loaded.state == State.NEW
        assert loaded.step_id == 0

    @pytest.mark.asyncio
    async def test_loaded_run_is_independent_copy(
        self, fresh_store, sample_run
    ):
        """Loading returns a copy; mutating it doesn't affect the store."""
        await fresh_store.save_checkpoint(sample_run)
        loaded1 = await fresh_store.load_latest(sample_run.run_id, sample_run.workspace_id)
        assert loaded1 is not None

        # Mutate the loaded copy
        loaded1.state = State.RESEARCHING

        # Load again — should still be the original
        loaded2 = await fresh_store.load_latest(sample_run.run_id, sample_run.workspace_id)
        assert loaded2 is not None
        assert loaded2.state == State.NEW


# ---------------------------------------------------------------------------
# 8. Idempotency (typed records — Correction 19)
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_insert_returns_true_for_new_key(self, fresh_store):
        rec = IdempotencyRecord(
            key="ik_1", workspace_id="ws_1", run_id="run_1",
            step_id=1, tool_name="send_email",
        )
        assert await fresh_store.insert_idempotency(rec) is True

    @pytest.mark.asyncio
    async def test_insert_returns_false_for_duplicate_key(self, fresh_store):
        rec = IdempotencyRecord(
            key="ik_1", workspace_id="ws_1", run_id="run_1",
            step_id=1, tool_name="send_email",
        )
        await fresh_store.insert_idempotency(rec)
        # Same key again — should return False (race condition resolution)
        assert await fresh_store.insert_idempotency(rec) is False

    @pytest.mark.asyncio
    async def test_get_returns_record(self, fresh_store):
        rec = IdempotencyRecord(
            key="ik_1", workspace_id="ws_1", run_id="run_1",
            step_id=1, tool_name="send_email",
        )
        await fresh_store.insert_idempotency(rec)
        loaded = await fresh_store.get_idempotency("ik_1")
        assert loaded is not None
        assert loaded.tool_name == "send_email"
        assert loaded.status == IdempotencyStatus.PENDING

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_key(self, fresh_store):
        assert await fresh_store.get_idempotency("nonexistent") is None

    @pytest.mark.asyncio
    async def test_update_changes_status(self, fresh_store):
        rec = IdempotencyRecord(
            key="ik_1", workspace_id="ws_1", run_id="run_1",
            step_id=1, tool_name="send_email",
        )
        await fresh_store.insert_idempotency(rec)
        result = await fresh_store.update_idempotency(
            "ik_1", IdempotencyStatus.DONE, result={"message_id": "msg_001"}
        )
        assert result is True
        loaded = await fresh_store.get_idempotency("ik_1")
        assert loaded is not None
        assert loaded.status == IdempotencyStatus.DONE
        assert loaded.result == {"message_id": "msg_001"}

    @pytest.mark.asyncio
    async def test_update_missing_key_returns_false(self, fresh_store):
        """Updating a non-existent key returns False (caller bug indicator)."""
        result = await fresh_store.update_idempotency(
            "nonexistent", IdempotencyStatus.DONE
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_update_with_error(self, fresh_store):
        rec = IdempotencyRecord(
            key="ik_1", workspace_id="ws_1", run_id="run_1",
            step_id=1, tool_name="send_email",
        )
        await fresh_store.insert_idempotency(rec)
        await fresh_store.update_idempotency(
            "ik_1", IdempotencyStatus.FAILED, error="Provider timeout"
        )
        loaded = await fresh_store.get_idempotency("ik_1")
        assert loaded is not None
        assert loaded.status == IdempotencyStatus.FAILED
        assert loaded.error == "Provider timeout"

    @pytest.mark.asyncio
    async def test_idempotency_record_is_typed(self, fresh_store):
        """Records are IdempotencyRecord instances, not dicts. (Correction 19.)"""
        rec = IdempotencyRecord(
            key="ik_1", workspace_id="ws_1", run_id="run_1",
            step_id=1, tool_name="send_email",
        )
        await fresh_store.insert_idempotency(rec)
        loaded = await fresh_store.get_idempotency("ik_1")
        assert loaded is not None
        assert isinstance(loaded, IdempotencyRecord)
        assert hasattr(loaded, "key")
        assert hasattr(loaded, "status")
        assert hasattr(loaded, "tool_name")
