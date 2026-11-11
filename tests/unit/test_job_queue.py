"""
Tests for opsmesh.job_queue (InMemoryJobQueue).

Covers:
  - enqueue + claim basic flow
  - claim returns None when empty
  - FIFO ordering
  - heartbeat renews lease
  - heartbeat from wrong worker rejected
  - complete marks job terminal
  - reclaim_stale reclaims expired leases
  - dead_letter moves to DLQ
  - workspace isolation on get()
  - depth and dlq_depth counting
  - attempts increment on each claim
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from opsmesh.job_queue import (
    DEFAULT_LEASE_SECONDS,
    InMemoryJobQueue,
    JobNotFound,
)
from opsmesh.models import JobStatus, utcnow


@pytest.fixture
def queue():
    return InMemoryJobQueue()


@pytest.fixture
def worker_id():
    return "worker_001"


# ---------------------------------------------------------------------------
# 1. Enqueue + claim
# ---------------------------------------------------------------------------


class TestEnqueueClaim:
    @pytest.mark.asyncio
    async def test_enqueue_returns_job_id(self, queue):
        job_id = await queue.enqueue("ws_1", "run_1")
        assert job_id.startswith("job_")

    @pytest.mark.asyncio
    async def test_claim_returns_job(self, queue, worker_id):
        await queue.enqueue("ws_1", "run_1")
        job = await queue.claim(worker_id)
        assert job is not None
        assert job.run_id == "run_1"
        assert job.status == JobStatus.RUNNING
        assert job.worker_id == worker_id
        assert job.lease_expires_at is not None

    @pytest.mark.asyncio
    async def test_claim_returns_none_when_empty(self, queue, worker_id):
        job = await queue.claim(worker_id)
        assert job is None

    @pytest.mark.asyncio
    async def test_claim_increments_attempts(self, queue, worker_id):
        """Each claim increments attempts (for DLQ threshold)."""
        await queue.enqueue("ws_1", "run_1")
        job = await queue.claim(worker_id)
        assert job.attempts == 1


# ---------------------------------------------------------------------------
# 2. FIFO ordering
# ---------------------------------------------------------------------------


class TestFIFOOrdering:
    @pytest.mark.asyncio
    async def test_jobs_claimed_in_enqueue_order(self, queue, worker_id):
        """The first job enqueued is the first claimed (FIFO)."""
        await queue.enqueue("ws_1", "run_1")
        await queue.enqueue("ws_1", "run_2")
        await queue.enqueue("ws_1", "run_3")

        job1 = await queue.claim(worker_id)
        job2 = await queue.claim(worker_id)
        job3 = await queue.claim(worker_id)

        assert job1.run_id == "run_1"
        assert job2.run_id == "run_2"
        assert job3.run_id == "run_3"

    @pytest.mark.asyncio
    async def test_completed_jobs_skipped(self, queue, worker_id):
        """Completed jobs are not re-claimed."""
        await queue.enqueue("ws_1", "run_1")
        await queue.enqueue("ws_1", "run_2")

        job1 = await queue.claim(worker_id)
        await queue.complete(job1.id, worker_id, JobStatus.COMPLETED)

        job2 = await queue.claim(worker_id)
        assert job2.run_id == "run_2"


# ---------------------------------------------------------------------------
# 3. Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_renews_lease(self, queue, worker_id):
        await queue.enqueue("ws_1", "run_1")
        job = await queue.claim(worker_id)
        original_lease = job.lease_expires_at

        # Wait a moment, then heartbeat
        import asyncio as _asyncio
        await _asyncio.sleep(0.01)
        await queue.heartbeat(job.id, worker_id)

        updated = await queue.get(job.id, "ws_1")
        assert updated is not None
        assert updated.lease_expires_at > original_lease

    @pytest.mark.asyncio
    async def test_heartbeat_wrong_worker_rejected(self, queue, worker_id):
        """A worker cannot heartbeat a job owned by another worker."""
        await queue.enqueue("ws_1", "run_1")
        job = await queue.claim(worker_id)
        with pytest.raises(Exception):
            await queue.heartbeat(job.id, "wrong_worker")

    @pytest.mark.asyncio
    async def test_heartbeat_unknown_job_raises(self, queue, worker_id):
        with pytest.raises(JobNotFound):
            await queue.heartbeat("nonexistent", worker_id)


# ---------------------------------------------------------------------------
# 4. Complete
# ---------------------------------------------------------------------------


class TestComplete:
    @pytest.mark.asyncio
    async def test_complete_marks_terminal(self, queue, worker_id):
        await queue.enqueue("ws_1", "run_1")
        job = await queue.claim(worker_id)
        await queue.complete(job.id, worker_id, JobStatus.COMPLETED)
        updated = await queue.get(job.id, "ws_1")
        assert updated.status == JobStatus.COMPLETED
        assert updated.completed_at is not None
        assert updated.worker_id is None  # lease released

    @pytest.mark.asyncio
    async def test_complete_failed_with_error(self, queue, worker_id):
        await queue.enqueue("ws_1", "run_1")
        job = await queue.claim(worker_id)
        await queue.complete(
            job.id, worker_id, JobStatus.FAILED, last_error="LLM timeout"
        )
        updated = await queue.get(job.id, "ws_1")
        assert updated.status == JobStatus.FAILED
        assert updated.last_error == "LLM timeout"

    @pytest.mark.asyncio
    async def test_complete_wrong_worker_rejected(self, queue, worker_id):
        await queue.enqueue("ws_1", "run_1")
        job = await queue.claim(worker_id)
        with pytest.raises(Exception):
            await queue.complete(job.id, "wrong_worker", JobStatus.COMPLETED)

    @pytest.mark.asyncio
    async def test_complete_invalid_status_rejected(self, queue, worker_id):
        await queue.enqueue("ws_1", "run_1")
        job = await queue.claim(worker_id)
        with pytest.raises(Exception):
            await queue.complete(job.id, worker_id, JobStatus.QUEUED)


# ---------------------------------------------------------------------------
# 5. Reclaim stale
# ---------------------------------------------------------------------------


class TestReclaimStale:
    @pytest.mark.asyncio
    async def test_reclaim_expired_lease(self, queue, worker_id):
        """A job whose lease has expired goes back to QUEUED."""
        await queue.enqueue("ws_1", "run_1")
        job = await queue.claim(worker_id, lease_seconds=1)

        # Simulate lease expiry by manually setting lease_expires_at in the past
        # (we'd need to wait 1s in real time, but manipulating the store is
        # cleaner for testing)
        async with queue._lock:
            queue._jobs[job.id].lease_expires_at = utcnow() - timedelta(seconds=1)

        reclaimed = await queue.reclaim_stale()
        assert reclaimed == 1

        updated = await queue.get(job.id, "ws_1")
        assert updated.status == JobStatus.QUEUED
        assert updated.worker_id is None

    @pytest.mark.asyncio
    async def test_reclaim_does_not_touch_running_jobs(self, queue, worker_id):
        """A job with a valid lease is NOT reclaimed."""
        await queue.enqueue("ws_1", "run_1")
        await queue.claim(worker_id, lease_seconds=300)  # 5 min lease
        reclaimed = await queue.reclaim_stale()
        assert reclaimed == 0

    @pytest.mark.asyncio
    async def test_reclaim_returns_count(self, queue, worker_id):
        """Multiple expired jobs are all reclaimed."""
        for i in range(3):
            await queue.enqueue("ws_1", f"run_{i}")
            job = await queue.claim(worker_id, lease_seconds=1)
            async with queue._lock:
                queue._jobs[job.id].lease_expires_at = utcnow() - timedelta(seconds=1)

        reclaimed = await queue.reclaim_stale()
        assert reclaimed == 3


# ---------------------------------------------------------------------------
# 6. Dead-letter queue
# ---------------------------------------------------------------------------


class TestDeadLetter:
    @pytest.mark.asyncio
    async def test_dead_letter_marks_status(self, queue):
        job_id = await queue.enqueue("ws_1", "run_1")
        await queue.dead_letter(job_id, "max retries exceeded")
        updated = await queue.get(job_id, "ws_1")
        assert updated.status == JobStatus.DEAD_LETTER
        assert "max retries" in (updated.last_error or "")

    @pytest.mark.asyncio
    async def test_dead_letter_unknown_job_raises(self, queue):
        with pytest.raises(JobNotFound):
            await queue.dead_letter("nonexistent", "test")

    @pytest.mark.asyncio
    async def test_dlq_depth_counts_dead_letter(self, queue):
        job_id = await queue.enqueue("ws_1", "run_1")
        await queue.dead_letter(job_id, "test")
        assert await queue.dlq_depth() == 1
        assert await queue.dlq_depth("ws_1") == 1
        assert await queue.dlq_depth("ws_2") == 0

    @pytest.mark.asyncio
    async def test_list_dlq(self, queue):
        job_id = await queue.enqueue("ws_1", "run_1")
        await queue.dead_letter(job_id, "test")
        dlq = await queue.list_dlq()
        assert len(dlq) == 1
        assert dlq[0].id == job_id


# ---------------------------------------------------------------------------
# 7. Workspace isolation
# ---------------------------------------------------------------------------


class TestWorkspaceIsolation:
    @pytest.mark.asyncio
    async def test_get_returns_none_for_cross_workspace(self, queue):
        job_id = await queue.enqueue("ws_1", "run_1")
        # Try to access from a different workspace
        result = await queue.get(job_id, "ws_2")
        assert result is None

    @pytest.mark.asyncio
    async def test_depth_scoped_to_workspace(self, queue):
        await queue.enqueue("ws_1", "run_1")
        await queue.enqueue("ws_1", "run_2")
        await queue.enqueue("ws_2", "run_3")
        assert await queue.depth() == 3
        assert await queue.depth("ws_1") == 2
        assert await queue.depth("ws_2") == 1


# ---------------------------------------------------------------------------
# 8. Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_lease_is_5_minutes(self):
        """Engineering Contract §1: job lease is 5 minutes."""
        assert DEFAULT_LEASE_SECONDS == 300
