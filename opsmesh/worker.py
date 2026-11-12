"""
OPSMESH Worker — the asyncio loop that processes jobs.

Architecture (course Ch 16 pro tip: "never run the supervisor inline"):
  The webhook returns 202 immediately. The worker is a separate process
  that polls the queue, claims jobs, and invokes the supervisor.

Phase 7 implementation:
  - Uses real DB-backed stores (same SQLite/Postgres as the API)
  - Uses real providers (Groq, Exa) via provider_factory
  - Uses real specialists (ResearchSpecialist, etc.)
  - Uses create_approval_store() so approvals are shared with the API
  - init_db() runs inside _main_async() (async — safe inside event loop)

Graceful shutdown (Engineering Contract §1):
  - SIGTERM → stop claiming, finish in-flight, exit within 30s.

Crash recovery:
  - On startup, reclaim_stale() picks up jobs whose leases expired
    (because the previous worker crashed).
  - The supervisor loads the latest checkpoint and resumes from there.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from opsmesh.job_queue import (
    DEFAULT_MAX_ATTEMPTS,
    InMemoryJobQueue,
    JobQueueProtocol,
)
from opsmesh.models import Job, JobStatus
from opsmesh.supervisor import Supervisor

logger = logging.getLogger(__name__)


# Polling interval: 1 second (Engineering Contract).
POLL_INTERVAL_SECONDS = 1.0


class Worker:
    """
    The background worker that processes jobs from the queue.

    Usage:
        worker = Worker(
            queue=InMemoryJobQueue(),
            supervisor=Supervisor(...),
            worker_id="worker_001",
        )
        await worker.start()  # runs until stop()
    """

    def __init__(
        self,
        queue: JobQueueProtocol,
        supervisor: Supervisor,
        worker_id: str = "worker_001",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        concurrency: int = 1,  # Day 3 adds a Semaphore for >1
    ) -> None:
        self.queue = queue
        self.supervisor = supervisor
        self.worker_id = worker_id
        self.max_attempts = max_attempts
        self.concurrency = concurrency
        self._shutting_down = False
        self._running = False
        self._processed_count = 0
        self._failed_count = 0
        self._task: Optional[asyncio.Task] = None

    @property
    def processed_count(self) -> int:
        return self._processed_count

    @property
    def failed_count(self) -> int:
        return self._failed_count

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start the worker loop. Runs until stop() is called."""
        # Reclaim any stale jobs from a previous (crashed) worker
        reclaimed = await self.queue.reclaim_stale()
        if reclaimed > 0:
            logger.info(f"Reclaimed {reclaimed} stale jobs on startup")

        self._running = True
        self._shutting_down = False

        while not self._shutting_down:
            try:
                await self._process_one_job()
            except Exception as e:
                # Log and continue — a single job failure shouldn't crash the worker
                logger.error(f"Worker error: {type(e).__name__}: {e}")

            # Wait before polling again
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        self._running = False

    async def stop(self) -> None:
        """Signal the worker to stop. Returns after the current job finishes."""
        self._shutting_down = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._running = False

    async def start_in_background(self) -> asyncio.Task:
        """Start the worker as a background task (for tests)."""
        self._task = asyncio.create_task(self.start())
        return self._task

    async def _process_one_job(self) -> None:
        """Claim a job, run the supervisor, complete or dead-letter."""
        job = await self.queue.claim(self.worker_id)
        if job is None:
            return  # queue empty

        try:
            # Build a job-like object for the supervisor
            class JobForSupervisor:
                run_id = job.run_id
                workspace_id = job.workspace_id
                lead_id = ""  # the supervisor loads from the run record

            await self.supervisor.run(JobForSupervisor())

            # Success
            await self.queue.complete(
                job.id, self.worker_id, JobStatus.COMPLETED
            )
            self._processed_count += 1
            logger.info(f"Completed job {job.id} (run {job.run_id})")

        except Exception as e:
            # Sanitized error (Correction 18)
            safe_msg = f"{type(e).__name__}: {str(e)[:200]}"
            logger.error(f"Job {job.id} failed: {safe_msg}")

            # Check if we should dead-letter or re-queue
            if job.attempts >= self.max_attempts:
                await self.queue.dead_letter(job.id, safe_msg)
                logger.warning(
                    f"Dead-lettered job {job.id} after {job.attempts} attempts"
                )
            else:
                await self.queue.complete(
                    job.id, self.worker_id, JobStatus.FAILED, last_error=safe_msg
                )
            self._failed_count += 1


# ---------------------------------------------------------------------------
# Executable entrypoint: `python -m opsmesh.worker`
# Starts the worker loop with graceful SIGTERM/SIGINT shutdown.
# ---------------------------------------------------------------------------


def _build_default_worker() -> "Worker":
    """Build a production-ready worker using real providers + DB.

    This factory:
      1. Reads Settings (from env vars / .env file)
      2. Calls configure_backend() SYNCHRONOUSLY (sets module-level state so
         create_lead_store() and create_approval_store() return the right
         stores). Does NOT connect to the DB yet — that happens in
         _init_worker_db() inside the event loop.
      3. Creates real providers (Groq, Exa) via provider_factory
      4. Creates real specialists (ResearchSpecialist, etc.)
      5. Creates the ApprovalGate with create_approval_store() (DB-backed
         when a DB is configured — so the API and worker share approvals)
      6. Returns a Worker ready to process jobs (after _init_worker_db() is called)

    The worker and API share the SAME database (via OPSMESH_DATABASE_PATH
    or OPSMESH_DATABASE_URL). The API enqueues jobs; the worker processes them.
    They also share the SAME approval store (DB-backed) so approvals created
    by the worker are visible to the API's /approvals endpoint.

    IMPORTANT: This function is SYNCHRONOUS. It does NOT call init_db() (which
    is async and must run inside an event loop). Call _init_worker_db()
    AFTER this, from inside an async function.
    """
    from opsmesh.config import get_settings
    from opsmesh.adapters.db.connection import configure_backend
    from opsmesh.adapters.db.lead_store import create_lead_store
    from opsmesh.adapters.provider_factory import (
        create_llm_provider,
        create_research_provider,
        create_email_provider,
        create_crm_provider,
    )
    from opsmesh.adapters.db.sqlite_state_store import SQLiteStateStore
    from opsmesh.adapters.db.sqlite_job_queue import SQLiteJobQueue
    from opsmesh.specialists import build_specialists
    from opsmesh.supervisor import Supervisor
    from opsmesh.approval_store import create_approval_store
    from opsmesh.approval_gate import ApprovalGate
    from opsmesh.adapters.notifier.mock import MockNotifier
    from opsmesh.state_store import InMemoryStateStore
    from opsmesh.job_queue import InMemoryJobQueue

    settings = get_settings()

    # 1. Configure the backend SYNCHRONOUSLY (sets _db_backend, _db_path, _db_url)
    #    This lets create_lead_store() and create_approval_store() return the
    #    right stores (DB-backed vs in-memory) without needing an event loop.
    #    The actual init_db() (connect + create schema) happens in _init_worker_db().
    configure_backend(
        database_path=settings.database_path or None,
        database_url=settings.database_url or None,
    )

    # 2. Build the store + queue based on the configured backend
    if settings.database_url or settings.database_path:
        # SQLite or Postgres — use the SQLiteStateStore (works with both via
        # the unified get_connection() abstraction)
        store = SQLiteStateStore()
        queue = SQLiteJobQueue()
    else:
        # In-memory (tests only — the worker won't see jobs from the API)
        store = InMemoryStateStore()
        queue = InMemoryJobQueue()

    # 3. Create real providers (LLM + research + email + CRM)
    llm = create_llm_provider(settings)
    research = create_research_provider(settings)
    email = create_email_provider(settings)
    crm = create_crm_provider(settings)
    lead_store = create_lead_store()

    # 4. Build real specialists (includes sending + crm_updating)
    #    Pass lead_store so specialists can load the real Lead data (company name,
    #    contact email, industry) — this makes research + draft use REAL data.
    specialists = build_specialists(
        llm=llm, settings=settings, research=research,
        email_provider=email, crm_provider=crm, lead_store=lead_store,
    )

    # 5. Build the ApprovalGate with create_approval_store()
    #    This is CRITICAL: the API and worker MUST share the same approval
    #    store. When a DB is configured, both use SQLiteApprovalStore which
    #    reads/writes the `approvals` table. Without this, the worker creates
    #    approvals in its own in-memory store, and the API can't see them.
    approval_store = create_approval_store()
    approval_gate = ApprovalGate(
        store=store,
        approval_store=approval_store,
        notifier=MockNotifier(),
    )

    # 6. Build the supervisor
    supervisor = Supervisor(
        specialists=specialists,
        store=store,
        approvals=approval_gate,
    )

    return Worker(
        queue=queue,
        supervisor=supervisor,
        worker_id=f"worker_{os.getpid()}",
    )


def build_in_process_supervisor(settings, store, approval_gate, lead_store,
                                sent_email_store=None, history_store=None):
    """Build a Supervisor for in-process worker mode.

    This reuses the SHARED store, approval_gate, lead_store, sent_email_store,
    and history_store from the API process so that leads, runs, approvals,
    sent-email logs, and history events are visible to BOTH the API endpoints
    and the worker's specialists.

    Used when OPSMESH_WORKER_IN_PROCESS is "auto" (and the store is
    in-memory) or "true". This avoids the need for a separate worker
    process + Postgres database on single-service deployments (e.g., a
    free-tier Render web service).

    Args:
        settings: the Settings singleton (already loaded).
        store: the SHARED StateStore (InMemoryStateStore or SQLiteStateStore).
        approval_gate: the SHARED ApprovalGate (contains the shared approval_store).
        lead_store: the SHARED LeadStore (so specialists see leads the API created).
        sent_email_store: the SHARED SentEmailStore (so sends are logged and
            visible in the Activity tab). Optional -- backward compat.

    Returns:
        A Supervisor ready to be passed to Worker(supervisor=...).
    """
    from opsmesh.adapters.provider_factory import (
        create_llm_provider,
        create_research_provider,
        create_email_provider,
        create_crm_provider,
    )
    from opsmesh.specialists import build_specialists
    from opsmesh.supervisor import Supervisor

    llm = create_llm_provider(settings)
    research = create_research_provider(settings)
    email = create_email_provider(settings)
    crm = create_crm_provider(settings)

    specialists = build_specialists(
        llm=llm,
        settings=settings,
        research=research,
        email_provider=email,
        crm_provider=crm,
        lead_store=lead_store,
        sent_email_store=sent_email_store,
        history_store=history_store,
    )

    return Supervisor(
        specialists=specialists,
        store=store,
        approvals=approval_gate,
    )


async def _init_worker_db() -> None:
    """Initialize the DB (async — must be called from inside an event loop).

    This connects to the DB + creates the schema (idempotent — CREATE TABLE
    IF NOT EXISTS). It uses the backend configured by _build_default_worker().

    This is the worker's equivalent of the API's lifespan handler — the
    place where async DB initialization happens safely inside the event loop.
    """
    from opsmesh.config import get_settings
    from opsmesh.adapters.db.connection import init_db

    settings = get_settings()
    if settings.database_url or settings.database_path:
        await init_db(
            database_path=settings.database_path or None,
            database_url=settings.database_url or None,
        )
        logger.info("worker.db_initialized", extra={
            "component": "worker",
            "database_path": settings.database_path or "(postgres)",
        })


async def _main_async():
    """Async entrypoint for the worker."""
    import signal

    # 1. Build the worker (synchronous — configures backend, creates providers)
    worker = _build_default_worker()

    # 2. Initialize the DB (async — connects + creates schema)
    #    This MUST happen inside the event loop (which we're in now).
    await _init_worker_db()

    logger.info("worker.starting", extra={
        "worker_id": worker.worker_id,
        "component": "worker",
        "operation": "startup",
    })

    # 3. Start the worker in the background
    task = await worker.start_in_background()

    # 4. Setup signal handlers for graceful shutdown
    stop_event = asyncio.Event()

    def _signal_handler(signum, frame):
        logger.info("worker.shutdown_signal", extra={
            "signal": signal.Signals(signum).name,
            "component": "worker",
        })
        stop_event.set()

    # Register handlers (only works in main thread)
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)
    except (ValueError, OSError):
        pass  # not in main thread — skip

    # 5. Wait for shutdown signal
    await stop_event.wait()

    # 6. Graceful shutdown
    logger.info("worker.stopping", extra={"component": "worker"})
    await worker.stop()
    logger.info("worker.stopped", extra={
        "component": "worker",
        "processed": worker.processed_count,
        "failed": worker.failed_count,
    })

    # 7. Close DB connections
    from opsmesh.adapters.db.connection import close_all
    await close_all()


if __name__ == "__main__":
    import asyncio
    import os

    # Configure structured logging
    from opsmesh.observability.logging import configure_logging, get_logger
    configure_logging()
    logger = get_logger("opsmesh.worker")

    asyncio.run(_main_async())


__all__ = ["Worker", "POLL_INTERVAL_SECONDS", "build_in_process_supervisor"]
