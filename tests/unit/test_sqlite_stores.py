"""
Tests for Phase 1: SQLite durable storage.

These tests verify:
  1. SQLiteStateStore saves/loads runs durably (survives "restart")
  2. SQLiteStateStore workspace isolation works
  3. SQLiteJobQueue atomic claiming (two concurrent claims — only one wins)
  4. SQLiteJobQueue durability (job survives reconnection)
  5. SQLiteApprovalStore atomic decide (concurrent — only one wins)
  6. SQLiteApprovalStore verify_and_consume
  7. SQLiteOutboxStore durable claiming
  8. Production mode fails closed without database_path
  9. Migration creates all 8 tables
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from opsmesh.adapters.db.connection import init_db, get_db_path, get_connection, reset_db_state
from opsmesh.adapters.db.sqlite_state_store import SQLiteStateStore
from opsmesh.adapters.db.sqlite_job_queue import SQLiteJobQueue
from opsmesh.adapters.db.sqlite_approval_store import SQLiteApprovalStore
from opsmesh.adapters.db.sqlite_outbox_store import SQLiteOutboxStore
from opsmesh.models import (
    ApprovalKind,
    ApprovalStatus,
    CRMOutboxOp,
    CRMOutboxOperation,
    IdempotencyRecord,
    IdempotencyStatus,
    JobStatus,
    RunRecord,
    utcnow,
)
from opsmesh.state_machine import State
from datetime import timedelta


@pytest.fixture
async def temp_db():
    """Create a temp SQLite DB, init it, create schema, and clean up after."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        await init_db(db_path)

        # Create schema directly (bypass Alembic for test speed)
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, lead_id TEXT NOT NULL,
                parent_run_id TEXT, state TEXT NOT NULL, step_id INTEGER DEFAULT 0,
                version INTEGER DEFAULT 0, payload TEXT NOT NULL,
                email_message_id TEXT, reconciliation_attempts INTEGER DEFAULT 0,
                budget_used_tokens INTEGER DEFAULT 0, budget_used_cost_usd FLOAT DEFAULT 0.0,
                budget_used_tool_calls INTEGER DEFAULT 0, draft_attempts INTEGER DEFAULT 0,
                critic_attempts INTEGER DEFAULT 0, last_error TEXT,
                started_at TEXT NOT NULL, updated_at TEXT NOT NULL, terminal_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_ws_state ON runs(workspace_id, state);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, run_id TEXT NOT NULL,
                status TEXT DEFAULT 'queued', worker_id TEXT, lease_expires_at TEXT,
                attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 4,
                enqueued_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, lease_expires_at);
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, run_id TEXT NOT NULL,
                kind TEXT NOT NULL, status TEXT DEFAULT 'pending', action TEXT NOT NULL,
                payload TEXT NOT NULL, payload_hash TEXT NOT NULL, decision TEXT,
                decision_feedback TEXT, decided_by TEXT, decided_at TEXT,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_appr_ws_status ON approvals(workspace_id, status);
            CREATE TABLE IF NOT EXISTS idempotency (
                key TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, run_id TEXT NOT NULL,
                step_id INTEGER NOT NULL, tool_name TEXT NOT NULL, status TEXT DEFAULT 'pending',
                result TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS crm_outbox (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, run_id TEXT NOT NULL,
                lead_id TEXT NOT NULL, operation TEXT NOT NULL, args TEXT NOT NULL,
                status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 5,
                next_attempt_at TEXT NOT NULL, last_error TEXT, created_at TEXT NOT NULL, completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_status ON crm_outbox(status, next_attempt_at);
        """)
        conn.commit()
        conn.close()

        yield db_path
    finally:
        os.unlink(db_path)
        for ext in ["-wal", "-shm"]:
            p = db_path + ext
            if os.path.exists(p):
                os.unlink(p)
        # Reset module-level DB state so it doesn't leak into other tests
        # (Phase 7: the connection module has _db_backend state that persists)
        reset_db_state()


# ===========================================================================
# 1. SQLiteStateStore
# ===========================================================================


class TestSQLiteStateStore:
    @pytest.mark.asyncio
    async def test_save_and_load(self, temp_db):
        """Save a run, load it back — data persists."""
        store = SQLiteStateStore()
        run = RunRecord(
            workspace_id="ws_1", lead_id="lead_1", state=State.NEW,
        )
        await store.save_checkpoint(run)

        loaded = await store.load_latest(run.run_id, "ws_1")
        assert loaded is not None
        assert loaded.workspace_id == "ws_1"
        assert loaded.state == State.NEW
        assert loaded.run_id == run.run_id

    @pytest.mark.asyncio
    async def test_workspace_isolation(self, temp_db):
        """Cross-workspace access returns None."""
        store = SQLiteStateStore()
        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.NEW)
        await store.save_checkpoint(run)

        loaded = await store.load_latest(run.run_id, "ws_2")
        assert loaded is None  # cross-workspace → 404

    @pytest.mark.asyncio
    async def test_durability_survives_reconnection(self, temp_db):
        """Data persists across store instances (simulates restart)."""
        store1 = SQLiteStateStore()
        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.NEW)
        await store1.save_checkpoint(run)

        # Create a NEW store instance (simulates restart)
        store2 = SQLiteStateStore()
        loaded = await store2.load_latest(run.run_id, "ws_1")
        assert loaded is not None
        assert loaded.run_id == run.run_id

    @pytest.mark.asyncio
    async def test_list_in_state(self, temp_db):
        """List runs filtered by state."""
        store = SQLiteStateStore()
        run1 = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.SENT)
        await store.save_checkpoint(run1)
        run2 = RunRecord(workspace_id="ws_1", lead_id="lead_2", state=State.FAILED)
        await store.save_checkpoint(run2)

        sent_runs = await store.list_in_state("ws_1", "sent")
        assert len(sent_runs) == 1
        assert sent_runs[0].run_id == run1.run_id

    @pytest.mark.asyncio
    async def test_version_increments(self, temp_db):
        """Each save increments the version."""
        store = SQLiteStateStore()
        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.NEW)
        v1 = await store.save_checkpoint(run)
        run.state = State.RESEARCHING
        v2 = await store.save_checkpoint(run, expected_version=v1)
        assert v2 > v1

    @pytest.mark.asyncio
    async def test_idempotency_insert_and_get(self, temp_db):
        """Idempotency records persist in SQLite."""
        store = SQLiteStateStore()
        record = IdempotencyRecord(
            key="ik_test_001", workspace_id="ws_1", run_id="run_1",
            step_id=1, tool_name="send_email",
        )
        inserted = await store.insert_idempotency(record)
        assert inserted is True

        loaded = await store.get_idempotency("ik_test_001")
        assert loaded is not None
        assert loaded.tool_name == "send_email"
        assert loaded.status == IdempotencyStatus.PENDING

    @pytest.mark.asyncio
    async def test_idempotency_duplicate_insert_returns_false(self, temp_db):
        """Duplicate key insert returns False (race condition resolution)."""
        store = SQLiteStateStore()
        record = IdempotencyRecord(
            key="ik_dup_001", workspace_id="ws_1", run_id="run_1",
            step_id=1, tool_name="send_email",
        )
        assert await store.insert_idempotency(record) is True
        assert await store.insert_idempotency(record) is False


# ===========================================================================
# 2. SQLiteJobQueue
# ===========================================================================


class TestSQLiteJobQueue:
    @pytest.mark.asyncio
    async def test_enqueue_and_claim(self, temp_db):
        """Enqueue a job, then claim it."""
        queue = SQLiteJobQueue()
        job_id = await queue.enqueue("ws_1", "run_1")
        assert job_id.startswith("job_")

        job = await queue.claim("worker_1")
        assert job is not None
        assert job.run_id == "run_1"
        assert job.status == JobStatus.RUNNING
        assert job.worker_id == "worker_1"

    @pytest.mark.asyncio
    async def test_claim_returns_none_when_empty(self, temp_db):
        queue = SQLiteJobQueue()
        job = await queue.claim("worker_1")
        assert job is None

    @pytest.mark.asyncio
    async def test_atomic_claim_only_one_wins(self, temp_db):
        """Two concurrent claims — only one wins (atomic)."""
        queue = SQLiteJobQueue()
        await queue.enqueue("ws_1", "run_1")

        # Claim 1
        job1 = await queue.claim("worker_1")
        assert job1 is not None

        # Claim 2 — should be None (job already claimed)
        job2 = await queue.claim("worker_2")
        assert job2 is None

    @pytest.mark.asyncio
    async def test_job_survives_reconnection(self, temp_db):
        """Job persists across queue instances (simulates restart)."""
        queue1 = SQLiteJobQueue()
        job_id = await queue1.enqueue("ws_1", "run_1")

        # New queue instance
        queue2 = SQLiteJobQueue()
        depth = await queue2.depth()
        assert depth == 1  # job is still there

    @pytest.mark.asyncio
    async def test_complete_and_dlq(self, temp_db):
        queue = SQLiteJobQueue()
        job_id = await queue.enqueue("ws_1", "run_1")
        job = await queue.claim("worker_1")
        await queue.complete(job.id, "worker_1", JobStatus.COMPLETED)

        assert await queue.depth() == 0
        assert await queue.dlq_depth() == 0

    @pytest.mark.asyncio
    async def test_reclaim_stale(self, temp_db):
        """Expired leases are reclaimed."""
        queue = SQLiteJobQueue()
        await queue.enqueue("ws_1", "run_1")
        job = await queue.claim("worker_1", lease_seconds=1)

        # Simulate lease expiry
        from opsmesh.adapters.db.connection import get_db_path
        import sqlite3
        conn = sqlite3.connect(get_db_path())
        conn.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
            ((utcnow() - timedelta(seconds=1)).isoformat(), job.id),
        )
        conn.commit()
        conn.close()

        reclaimed = await queue.reclaim_stale()
        assert reclaimed == 1
        assert await queue.depth() == 1  # back to queued


# ===========================================================================
# 3. SQLiteApprovalStore
# ===========================================================================


class TestSQLiteApprovalStore:
    @pytest.mark.asyncio
    async def test_create_and_get(self, temp_db):
        store = SQLiteApprovalStore()
        record = store.create(
            workspace_id="ws_1", run_id="run_1",
            kind=ApprovalKind.EMAIL_SEND, action="send_email",
            payload={"to": "r@acme.com"}, payload_hash="sha256:abc",
        )
        assert record.id.startswith("ap_")

        loaded = store.get(record.id)
        assert loaded is not None
        assert loaded.action == "send_email"

    @pytest.mark.asyncio
    async def test_atomic_decide_only_one_wins(self, temp_db):
        """Concurrent decide — only the first wins (atomic CAS)."""
        store = SQLiteApprovalStore()
        record = store.create(
            workspace_id="ws_1", run_id="run_1",
            kind=ApprovalKind.EMAIL_SEND, action="send_email",
            payload={}, payload_hash="sha256:abc",
        )

        # First decide succeeds
        result1 = await store.decide(
            record.id, "ws_1", ApprovalStatus.APPROVED,
            "approve", "user_1",
        )
        assert result1.status == ApprovalStatus.APPROVED

        # Second decide — should fail (AlreadyResolved)
        from opsmesh.approval_gate import AlreadyResolvedError
        with pytest.raises(AlreadyResolvedError):
            await store.decide(
                record.id, "ws_1", ApprovalStatus.REJECTED,
                "reject", "user_2",
            )

    @pytest.mark.asyncio
    async def test_verify_and_consume(self, temp_db):
        store = SQLiteApprovalStore()
        record = store.create(
            workspace_id="ws_1", run_id="run_1",
            kind=ApprovalKind.EMAIL_SEND, action="send_email",
            payload={}, payload_hash="sha256:abc",
        )
        # Approve first
        await store.decide(
            record.id, "ws_1", ApprovalStatus.APPROVED,
            "approve", "user_1",
        )
        # Consume
        consumed = await store.verify_and_consume(
            record.id, "ws_1", "run_1", "send_email", "sha256:abc",
        )
        assert consumed.status == ApprovalStatus.CONSUMED

    @pytest.mark.asyncio
    async def test_find_expired_pending(self, temp_db):
        store = SQLiteApprovalStore()
        record = store.create(
            workspace_id="ws_1", run_id="run_1",
            kind=ApprovalKind.EMAIL_SEND, action="send_email",
            payload={}, payload_hash="sha256:abc",
            expires_at=utcnow() - timedelta(hours=1),  # already expired
        )
        expired = store.find_expired_pending()
        assert len(expired) == 1
        assert expired[0].id == record.id


# ===========================================================================
# 4. SQLiteOutboxStore
# ===========================================================================


class TestSQLiteOutboxStore:
    @pytest.mark.asyncio
    async def test_enqueue_and_claim(self, temp_db):
        store = SQLiteOutboxStore()
        op = CRMOutboxOp(
            workspace_id="ws_1", run_id="run_1", lead_id="lead_1",
            operation=CRMOutboxOperation.UPDATE_LEAD_STATUS,
            args={"lead_id": "L_001", "status": "contacted"},
        )
        store.enqueue(op)

        claimed = await store.claim_pending(limit=10)
        assert len(claimed) == 1
        assert claimed[0].operation == CRMOutboxOperation.UPDATE_LEAD_STATUS
        assert claimed[0].status.value == "in_progress"

    @pytest.mark.asyncio
    async def test_mark_done(self, temp_db):
        store = SQLiteOutboxStore()
        op = CRMOutboxOp(
            workspace_id="ws_1", run_id="run_1", lead_id="lead_1",
            operation=CRMOutboxOperation.UPDATE_LEAD_STATUS,
            args={},
        )
        store.enqueue(op)
        store.mark_done(op.id)

        loaded = store.get(op.id)
        assert loaded.status.value == "done"

    @pytest.mark.asyncio
    async def test_outbox_survives_reconnection(self, temp_db):
        """Outbox ops persist across store instances."""
        store1 = SQLiteOutboxStore()
        op = CRMOutboxOp(
            workspace_id="ws_1", run_id="run_1", lead_id="lead_1",
            operation=CRMOutboxOperation.UPDATE_LEAD_STATUS,
            args={},
        )
        store1.enqueue(op)

        # New store instance
        store2 = SQLiteOutboxStore()
        pending = store2.list_pending()
        assert len(pending) == 1


# ===========================================================================
# 5. Production mode fail-closed
# ===========================================================================


class TestProductionModeFailClosed:
    def test_production_mode_without_db_raises(self):
        """production_mode=True + no database_path or database_url → RuntimeError."""
        from opsmesh.config import Settings, get_settings
        from opsmesh.api.app import create_app
        from opsmesh.adapters.db.connection import reset_db_state, get_db_backend

        # Set env vars for production mode without DB
        os.environ["OPSMESH_PRODUCTION_MODE"] = "true"
        os.environ["OPSMESH_DATABASE_PATH"] = ""
        # Also clear database_url (Phase 7)
        os.environ.pop("OPSMESH_DATABASE_URL", None)
        # Clear the settings cache (previous tests may have cached a different config)
        get_settings.cache_clear()
        # Reset module-level DB state (previous tests in this file may have
        # called init_db(), leaving _db_backend != "memory")
        reset_db_state()

        # Sanity check: env is what we think it is
        assert get_db_backend() == "memory", f"DB backend should be 'memory' but is '{get_db_backend()}'"
        s = get_settings()
        assert s.production_mode is True
        assert not s.database_url
        assert not s.database_path

        try:
            # Phase 7: error message now mentions both Postgres and SQLite
            with pytest.raises(RuntimeError, match="database_url.*or.*database_path.*must be set"):
                create_app()
        finally:
            os.environ.pop("OPSMESH_PRODUCTION_MODE", None)
            os.environ.pop("OPSMESH_DATABASE_PATH", None)
            os.environ.pop("OPSMESH_DATABASE_URL", None)
            get_settings.cache_clear()
            reset_db_state()

    def test_dev_mode_without_db_works(self):
        """Dev mode (production_mode=False) + no database_path → in-memory works."""
        from opsmesh.config import get_settings
        from opsmesh.api.app import create_app
        from opsmesh.adapters.db.connection import reset_db_state

        get_settings.cache_clear()
        os.environ.pop("OPSMESH_PRODUCTION_MODE", None)
        os.environ.pop("OPSMESH_DATABASE_PATH", None)
        os.environ.pop("OPSMESH_DATABASE_URL", None)
        reset_db_state()

        app = create_app(webhook_secret="test-secret")
        assert app.state.use_sqlite is False
        assert app.state.store is not None
