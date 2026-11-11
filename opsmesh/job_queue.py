"""
OPSMESH JobQueue — the durable work queue.

Architecture (ADR-004: Postgres-backed job queue with FOR UPDATE SKIP LOCKED):
  The queue is a TABLE in the same Postgres as the run state. This is not
  Redis, not Celery, not RabbitMQ — it's Postgres. Why:
    1. No new infrastructure (Postgres is already required for run state).
    2. Jobs are durable (survive worker crash).
    3. Job enqueue + run creation can be in the SAME transaction.
    4. FOR UPDATE SKIP LOCKED is the standard Postgres pattern for
       concurrent queue claiming — well-understood, no race conditions.
    5. The jobs table is inspectable via SQL (dashboard queries it directly).

Day 2 implementation: InMemoryJobQueue.
  The in-memory version mirrors the Postgres contract but uses asyncio.Lock
  instead of FOR UPDATE SKIP LOCKED. The interface is identical — Day 6's
  PostgresJobQueue will be a drop-in replacement.

Worker lease model:
  When a worker claims a job, it gets a lease (default 5 minutes). The worker
  must heartbeat (renew the lease) every ~60s while processing. If the worker
  crashes, the lease expires and another worker can reclaim the job.

Dead-letter queue:
  After max_attempts (default 4), a job is moved to status='dead_letter'.
  The operator can replay it via POST /api/v1/dlq/{id}/replay (Day 5).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Optional, Protocol, runtime_checkable

from opsmesh.models import Job, JobStatus, utcnow


# Default lease duration: 5 minutes (Engineering Contract §1).
# A worker that hasn't heartbeated in 5 min is presumed dead.
DEFAULT_LEASE_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 4


class JobQueueError(Exception):
    """Base class for JobQueue errors."""


class JobNotFound(JobQueueError):
    """Raised when a job_id doesn't exist or doesn't belong to the workspace."""


@runtime_checkable
class JobQueueProtocol(Protocol):
    """The job queue contract. Day 6's PostgresJobQueue implements this."""

    async def enqueue(
        self,
        workspace_id: str,
        run_id: str,
    ) -> str:
        """Add a job to the queue. Returns the job_id."""
        ...

    async def claim(
        self, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> Optional[Job]:
        """Atomically claim the next available job. Returns None if queue
        is empty. Uses FOR UPDATE SKIP LOCKED in Postgres; asyncio.Lock here."""
        ...

    async def heartbeat(self, job_id: str, worker_id: str) -> None:
        """Renew the lease. The worker must call this every ~60s while
        processing, or the lease expires and another worker can reclaim."""
        ...

    async def complete(
        self, job_id: str, worker_id: str, status: JobStatus, last_error: Optional[str] = None
    ) -> None:
        """Mark a job complete (status=COMPLETED or FAILED). Releases the lease."""
        ...

    async def reclaim_stale(self) -> int:
        """Reclaim jobs whose leases have expired. Returns the count reclaimed.
        Called periodically by the worker on startup and every ~60s."""
        ...

    async def dead_letter(self, job_id: str, reason: str) -> None:
        """Move a job to the dead-letter queue (status=DEAD_LETTER)."""
        ...

    async def get(self, job_id: str, workspace_id: str) -> Optional[Job]:
        """Fetch a job by ID. Returns None if not found OR cross-workspace."""
        ...

    async def depth(self, workspace_id: Optional[str] = None) -> int:
        """Number of queued jobs. If workspace_id given, scoped to that workspace."""
        ...

    async def dlq_depth(self, workspace_id: Optional[str] = None) -> int:
        """Number of dead-lettered jobs."""
        ...


# ---------------------------------------------------------------------------
# InMemoryJobQueue — Day 2 implementation.
# ---------------------------------------------------------------------------


class InMemoryJobQueue:
    """
    In-memory job queue. Mirrors the Postgres contract.

    Uses asyncio.Lock instead of FOR UPDATE SKIP LOCKED. Single-event-loop
    safe; NOT multi-process safe. For multi-process, use PostgresJobQueue
    (Day 6).

    The queue is FIFO within a workspace: jobs are claimed oldest-first.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()
        # Index for fast depth queries
        self._queued_order: list[str] = []  # job_ids in enqueue order

    async def enqueue(
        self,
        workspace_id: str,
        run_id: str,
    ) -> str:
        """Add a job to the queue. Returns the job_id."""
        async with self._lock:
            job = Job(
                workspace_id=workspace_id,
                run_id=run_id,
                status=JobStatus.QUEUED,
            )
            self._jobs[job.id] = job
            self._queued_order.append(job.id)
            return job.id

    async def claim(
        self, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> Optional[Job]:
        """Atomically claim the next available job.

        Scans _queued_order for the first job with status=QUEUED. Marks it
        RUNNING, sets worker_id and lease_expires_at, and returns a copy.

        Returns None if no queued jobs exist.
        """
        async with self._lock:
            for job_id in self._queued_order:
                job = self._jobs[job_id]
                if job.status == JobStatus.QUEUED:
                    job.status = JobStatus.RUNNING
                    job.worker_id = worker_id
                    job.started_at = utcnow()
                    job.lease_expires_at = utcnow() + timedelta(seconds=lease_seconds)
                    job.attempts += 1
                    self._jobs[job_id] = job
                    return job.model_copy(deep=True)
            return None

    async def heartbeat(self, job_id: str, worker_id: str) -> None:
        """Renew the lease. Worker must own the job (worker_id matches)."""
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(f"Job {job_id} not found")
            if job.worker_id != worker_id:
                raise JobQueueError(
                    f"Worker {worker_id} cannot heartbeat job {job_id} "
                    f"owned by {job.worker_id}"
                )
            job.lease_expires_at = utcnow() + timedelta(seconds=DEFAULT_LEASE_SECONDS)
            self._jobs[job_id] = job

    async def complete(
        self,
        job_id: str,
        worker_id: str,
        status: JobStatus,
        last_error: Optional[str] = None,
    ) -> None:
        """Mark a job complete. Status must be COMPLETED or FAILED."""
        if status not in (JobStatus.COMPLETED, JobStatus.FAILED):
            raise JobQueueError(
                f"complete() requires COMPLETED or FAILED, got {status}"
            )
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(f"Job {job_id} not found")
            if job.worker_id != worker_id:
                raise JobQueueError(
                    f"Worker {worker_id} cannot complete job {job_id} "
                    f"owned by {job.worker_id}"
                )
            job.status = status
            job.completed_at = utcnow()
            job.lease_expires_at = None
            job.worker_id = None
            if last_error is not None:
                job.last_error = last_error
            self._jobs[job_id] = job
            # Remove from queued_order index if present
            if job_id in self._queued_order:
                self._queued_order.remove(job_id)

    async def reclaim_stale(self) -> int:
        """Reclaim jobs whose leases have expired. The job goes back to
        QUEUED, worker_id is cleared, attempts is NOT incremented (we
        only increment attempts on actual claim)."""
        async with self._lock:
            now = utcnow()
            count = 0
            for job in self._jobs.values():
                if (
                    job.status == JobStatus.RUNNING
                    and job.lease_expires_at is not None
                    and job.lease_expires_at < now
                ):
                    job.status = JobStatus.QUEUED
                    job.worker_id = None
                    job.lease_expires_at = None
                    job.last_error = "lease expired; reclaimed"
                    count += 1
            return count

    async def dead_letter(self, job_id: str, reason: str) -> None:
        """Move a job to the dead-letter queue."""
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(f"Job {job_id} not found")
            job.status = JobStatus.DEAD_LETTER
            job.last_error = reason
            job.lease_expires_at = None
            job.worker_id = None
            self._jobs[job_id] = job
            if job_id in self._queued_order:
                self._queued_order.remove(job_id)

    async def get(self, job_id: str, workspace_id: str) -> Optional[Job]:
        """Fetch a job by ID. Returns None if not found OR cross-workspace."""
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.workspace_id != workspace_id:
                return None
            return job.model_copy(deep=True)

    async def depth(self, workspace_id: Optional[str] = None) -> int:
        """Number of QUEUED jobs."""
        async with self._lock:
            if workspace_id is None:
                return sum(
                    1 for j in self._jobs.values() if j.status == JobStatus.QUEUED
                )
            return sum(
                1 for j in self._jobs.values()
                if j.status == JobStatus.QUEUED and j.workspace_id == workspace_id
            )

    async def dlq_depth(self, workspace_id: Optional[str] = None) -> int:
        """Number of DEAD_LETTER jobs."""
        async with self._lock:
            if workspace_id is None:
                return sum(
                    1 for j in self._jobs.values() if j.status == JobStatus.DEAD_LETTER
                )
            return sum(
                1 for j in self._jobs.values()
                if j.status == JobStatus.DEAD_LETTER and j.workspace_id == workspace_id
            )

    async def list_dlq(self, workspace_id: Optional[str] = None) -> list[Job]:
        """List dead-lettered jobs (for the DLQ dashboard)."""
        async with self._lock:
            jobs = [
                j.model_copy(deep=True)
                for j in self._jobs.values()
                if j.status == JobStatus.DEAD_LETTER
            ]
            if workspace_id is not None:
                jobs = [j for j in jobs if j.workspace_id == workspace_id]
            return jobs


__all__ = [
    "JobQueueProtocol",
    "JobQueueError",
    "JobNotFound",
    "InMemoryJobQueue",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
]
