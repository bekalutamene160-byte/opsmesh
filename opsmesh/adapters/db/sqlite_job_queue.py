"""
OPSMESH SQLite JobQueue — durable implementation of JobQueueProtocol.

Implements atomic job claiming using SQLite's BEGIN IMMEDIATE transaction.
No FOR UPDATE SKIP LOCKED (doesn't exist in SQLite).

CONCURRENCY STRATEGY:
  claim(): BEGIN IMMEDIATE TRANSACTION
              UPDATE jobs SET status='running', worker_id=?, lease_expires_at=?
              WHERE id = (
                  SELECT id FROM jobs WHERE status='queued'
                  ORDER BY enqueued_at LIMIT 1
              )
            COMMIT

  BEGIN IMMEDIATE acquires the write lock immediately. If another worker
  is already writing, this one waits (busy_timeout=5000). The subquery +
  UPDATE is atomic within the transaction — no race condition.

USAGE:
    from opsmesh.adapters.db.sqlite_job_queue import SQLiteJobQueue
    queue = SQLiteJobQueue()
    job_id = await queue.enqueue("ws_1", "run_1")
    job = await queue.claim("worker_1")
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from opsmesh.adapters.db.connection import get_connection
from opsmesh.job_queue import DEFAULT_LEASE_SECONDS, JobQueueProtocol
from opsmesh.models import Job, JobStatus, utcnow


class SQLiteJobQueue(JobQueueProtocol):
    """
    SQLite-backed job queue. Durable — jobs survive process restarts.
    """

    async def enqueue(
        self, workspace_id: str, run_id: str
    ) -> str:
        """Add a job to the queue."""
        job = Job(workspace_id=workspace_id, run_id=run_id)
        async with get_connection() as conn:
            await conn.execute(
                """INSERT INTO jobs
                   (id, workspace_id, run_id, status, worker_id, lease_expires_at,
                    attempts, max_attempts, enqueued_at, started_at, completed_at,
                    last_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.id, job.workspace_id, job.run_id,
                    job.status.value, job.worker_id, None,
                    job.attempts, job.max_attempts,
                    job.enqueued_at.isoformat(), None, None, None,
                ),
            )
            await conn.commit()
        return job.id

    async def claim(
        self, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> Optional[Job]:
        """Atomically claim the next available job using BEGIN IMMEDIATE.

        This is the SQLite alternative to PostgreSQL's FOR UPDATE SKIP LOCKED.
        BEGIN IMMEDIATE acquires the write lock, preventing concurrent writers.
        """
        async with get_connection() as conn:
            # BEGIN IMMEDIATE — acquire the write lock
            await conn.execute("BEGIN IMMEDIATE TRANSACTION")

            try:
                # Find the next queued job
                cursor = await conn.execute(
                    "SELECT id, workspace_id, run_id, status, worker_id, "
                    "lease_expires_at, attempts, max_attempts, enqueued_at, "
                    "started_at, completed_at, last_error "
                    "FROM jobs WHERE status = 'queued' "
                    "ORDER BY enqueued_at LIMIT 1"
                )
                row = await cursor.fetchone()
                await cursor.close()

                if row is None:
                    await conn.execute("COMMIT")
                    return None

                job_id = row[0]
                lease_expires = utcnow() + timedelta(seconds=lease_seconds)

                # Claim it
                await conn.execute(
                    """UPDATE jobs SET
                        status = 'running', worker_id = ?,
                        lease_expires_at = ?, started_at = ?,
                        attempts = attempts + 1
                       WHERE id = ? AND status = 'queued'""",
                    (worker_id, lease_expires.isoformat(),
                     utcnow().isoformat(), job_id),
                )
                await conn.execute("COMMIT")

                # Return the claimed job
                return Job(
                    id=job_id, workspace_id=row[1], run_id=row[2],
                    status=JobStatus.RUNNING, worker_id=worker_id,
                    lease_expires_at=lease_expires,
                    attempts=row[6] + 1, max_attempts=row[7],
                    enqueued_at=row[8] if isinstance(row[8], str) else utcnow(),
                )

            except Exception:
                await conn.execute("ROLLBACK")
                raise

    async def heartbeat(self, job_id: str, worker_id: str) -> None:
        """Renew the lease."""
        from opsmesh.job_queue import JobNotFound, JobQueueError
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT worker_id FROM jobs WHERE id = ?",
                (job_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()

            if row is None:
                raise JobNotFound(f"Job {job_id} not found")
            if row[0] != worker_id:
                raise JobQueueError(
                    f"Worker {worker_id} cannot heartbeat job {job_id} "
                    f"owned by {row[0]}"
                )

            lease_expires = utcnow() + timedelta(seconds=DEFAULT_LEASE_SECONDS)
            await conn.execute(
                "UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
                (lease_expires.isoformat(), job_id),
            )
            await conn.commit()

    async def complete(
        self, job_id: str, worker_id: str, status: JobStatus,
        last_error: Optional[str] = None
    ) -> None:
        """Mark a job complete."""
        from opsmesh.job_queue import JobNotFound, JobQueueError
        if status not in (JobStatus.COMPLETED, JobStatus.FAILED):
            from opsmesh.job_queue import JobQueueError as JQE
            raise JQE(f"complete() requires COMPLETED or FAILED, got {status}")

        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT worker_id FROM jobs WHERE id = ?",
                (job_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()

            if row is None:
                raise JobNotFound(f"Job {job_id} not found")
            if row[0] != worker_id:
                raise JobQueueError(
                    f"Worker {worker_id} cannot complete job {job_id}"
                )

            await conn.execute(
                """UPDATE jobs SET
                    status = ?, completed_at = ?, lease_expires_at = NULL,
                    worker_id = NULL, last_error = ?
                   WHERE id = ?""",
                (status.value, utcnow().isoformat(), last_error, job_id),
            )
            await conn.commit()

    async def reclaim_stale(self) -> int:
        """Reclaim jobs whose leases have expired."""
        now = utcnow().isoformat()
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'running' "
                "AND lease_expires_at < ?",
                (now,),
            )
            count_row = await cursor.fetchone()
            await cursor.close()

            await conn.execute(
                """UPDATE jobs SET
                    status = 'queued', worker_id = NULL,
                    lease_expires_at = NULL,
                    last_error = 'lease expired; reclaimed'
                   WHERE status = 'running' AND lease_expires_at < ?""",
                (now,),
            )
            await conn.commit()
            return count_row[0] if count_row else 0

    async def dead_letter(self, job_id: str, reason: str) -> None:
        """Move a job to the dead-letter queue."""
        from opsmesh.job_queue import JobNotFound
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM jobs WHERE id = ?",
                (job_id,),
            )
            if await cursor.fetchone() is None:
                await cursor.close()
                raise JobNotFound(f"Job {job_id} not found")
            await cursor.close()

            await conn.execute(
                """UPDATE jobs SET
                    status = 'dead_letter', last_error = ?,
                    lease_expires_at = NULL, worker_id = NULL
                   WHERE id = ?""",
                (reason, job_id),
            )
            await conn.commit()

    async def get(self, job_id: str, workspace_id: str) -> Optional[Job]:
        """Fetch a job by ID. Returns None if not found OR cross-workspace."""
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT id, workspace_id, run_id, status, worker_id, "
                "lease_expires_at, attempts, max_attempts, enqueued_at, "
                "started_at, completed_at, last_error "
                "FROM jobs WHERE id = ? AND workspace_id = ?",
                (job_id, workspace_id),
            )
            row = await cursor.fetchone()
            await cursor.close()

            if row is None:
                return None

            from datetime import datetime
            def parse_dt(s):
                if s is None:
                    return None
                return datetime.fromisoformat(s)

            return Job(
                id=row[0], workspace_id=row[1], run_id=row[2],
                status=JobStatus(row[3]), worker_id=row[4],
                lease_expires_at=parse_dt(row[5]),
                attempts=row[6], max_attempts=row[7],
                enqueued_at=parse_dt(row[8]) or utcnow(),
                started_at=parse_dt(row[9]),
                completed_at=parse_dt(row[10]),
                last_error=row[11],
            )

    async def depth(self, workspace_id: Optional[str] = None) -> int:
        """Number of queued jobs."""
        async with get_connection() as conn:
            if workspace_id is None:
                cursor = await conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status = 'queued'"
                )
            else:
                cursor = await conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status = 'queued' "
                    "AND workspace_id = ?",
                    (workspace_id,),
                )
            row = await cursor.fetchone()
            await cursor.close()
            return row[0] if row else 0

    async def dlq_depth(self, workspace_id: Optional[str] = None) -> int:
        """Number of dead-lettered jobs."""
        async with get_connection() as conn:
            if workspace_id is None:
                cursor = await conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status = 'dead_letter'"
                )
            else:
                cursor = await conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status = 'dead_letter' "
                    "AND workspace_id = ?",
                    (workspace_id,),
                )
            row = await cursor.fetchone()
            await cursor.close()
            return row[0] if row else 0

    async def list_dlq(self, workspace_id: Optional[str] = None) -> list[Job]:
        """List dead-lettered jobs."""
        async with get_connection() as conn:
            if workspace_id is None:
                cursor = await conn.execute(
                    "SELECT id, workspace_id, run_id, status, worker_id, "
                    "lease_expires_at, attempts, max_attempts, enqueued_at, "
                    "started_at, completed_at, last_error "
                    "FROM jobs WHERE status = 'dead_letter' "
                    "ORDER BY completed_at DESC"
                )
            else:
                cursor = await conn.execute(
                    "SELECT id, workspace_id, run_id, status, worker_id, "
                    "lease_expires_at, attempts, max_attempts, enqueued_at, "
                    "started_at, completed_at, last_error "
                    "FROM jobs WHERE status = 'dead_letter' "
                    "AND workspace_id = ? ORDER BY completed_at DESC",
                    (workspace_id,),
                )
            rows = await cursor.fetchall()
            await cursor.close()

            from datetime import datetime
            def parse_dt(s):
                if s is None:
                    return None
                return datetime.fromisoformat(s)

            return [
                Job(
                    id=row[0], workspace_id=row[1], run_id=row[2],
                    status=JobStatus(row[3]), worker_id=row[4],
                    lease_expires_at=parse_dt(row[5]),
                    attempts=row[6], max_attempts=row[7],
                    enqueued_at=parse_dt(row[8]) or utcnow(),
                    started_at=parse_dt(row[9]),
                    completed_at=parse_dt(row[10]),
                    last_error=row[11],
                )
                for row in rows
            ]


__all__ = ["SQLiteJobQueue"]
