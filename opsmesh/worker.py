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
        reconcilers: Optional[list] = None,  # Stage B Phase 3
    ) -> None:
        self.queue = queue
        self.supervisor = supervisor
        self.worker_id = worker_id
        self.max_attempts = max_attempts
        self.concurrency = concurrency
        # Stage B Phase 3: background reconcilers (EmailSendReconciler,
        # ApprovalTimeoutReconciler, CRMOutboxDrainer). Started/stopped
        # alongside the job loop so recovery + timeout run continuously.
        self.reconcilers = reconcilers or []
        self._reconciler_tasks: list[asyncio.Task] = []
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

    def status(self) -> dict:
        """Stage B Phase 3: snapshot of worker + reconciler state for the
        /worker/status endpoint."""
        return {
            "worker_id": self.worker_id,
            "running": self._running,
            "processed_count": self._processed_count,
            "failed_count": self._failed_count,
            "reconcilers": [
                {
                    "name": type(r).__name__,
                    "running": getattr(r, "_running", False),
                }
                for r in self.reconcilers
            ],
        }

    async def start(self) -> None:
        """Start the worker loop. Runs until stop() is called.

        Stage B Phase 3: also starts the background reconcilers (email-send
        recovery, approval-timeout expiry, CRM-outbox draining)."""
        # Reclaim any stale jobs from a previous (crashed) worker
        reclaimed = await self.queue.reclaim_stale()
        if reclaimed > 0:
            logger.info(f"Reclaimed {reclaimed} stale jobs on startup")

        # Start the reconcilers as background tasks.
        self._reconciler_tasks = []
        for r in self.reconcilers:
            try:
                task = await r.start_in_background()
                self._reconciler_tasks.append(task)
                logger.info(f"Started reconciler {type(r).__name__}")
            except Exception as e:
                logger.warning(f"Failed to start reconciler {type(r).__name__}: {e}")

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
        """Signal the worker to stop. Returns after the current job finishes.

        Stage B Phase 3: also stops the background reconcilers."""
        self._shutting_down = True
        # Stop the reconcilers.
        for r in self.reconcilers:
            try:
                await r.stop()
            except Exception:
                pass
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

    # 4. Build the ApprovalGate with create_approval_store() FIRST (the
    #    gateway needs the approval_store for verify_and_consume).
    approval_store = create_approval_store()
    approval_gate = ApprovalGate(
        store=store,
        approval_store=approval_store,
        notifier=MockNotifier(),
    )

    # 5. Stage B Phase 2 (P0-1): build the ToolGateway wired to the email +
    #    CRM providers. Specialists route side effects through it so the
    #    7-check policy layer (approval verify-and-consume, payload hash
    #    binding) is enforced at the actual send/sync site.
    gateway = build_tool_gateway(
        settings=settings,
        store=store,
        approval_store=approval_store,
        email_provider=email,
        crm_provider=crm,
    )

    # 6. Stage B Phase 3: build the CRM outbox + the 3 reconcilers. The
    #    outbox decouples CRM sync from the email send; the drainer claims +
    #    drains pending ops. EmailSendReconciler resolves SEND_UNKNOWN runs;
    #    ApprovalTimeoutReconciler expires 24h-old pending approvals.
    from opsmesh.reconciler.email_send import EmailSendReconciler
    from opsmesh.reconciler.approval_timeout import ApprovalTimeoutReconciler
    from opsmesh.reconciler.crm_outbox import CRMOutboxStore, CRMOutboxDrainer

    outbox_store = CRMOutboxStore()
    reconcilers: list = []
    if email is not None:
        reconcilers.append(EmailSendReconciler(
            store=store, email_provider=email,
            default_workspace_id=getattr(settings, "demo_workspace_id", "default"),
        ))
    reconcilers.append(ApprovalTimeoutReconciler(
        store=store, approval_store=approval_store,
    ))
    if crm is not None:
        reconcilers.append(CRMOutboxDrainer(crm_provider=crm, outbox_store=outbox_store))

    # 7. Build real specialists (includes sending + crm_updating), wired to
    #    the gateway + lead_store + outbox so they load REAL lead data and
    #    enqueue CRM ops through the outbox.
    specialists = build_specialists(
        llm=llm, settings=settings, research=research,
        email_provider=email, crm_provider=crm, lead_store=lead_store,
        gateway=gateway, outbox_store=outbox_store,
    )

    # 8. Build the supervisor
    supervisor = Supervisor(
        specialists=specialists,
        store=store,
        approvals=approval_gate,
        gateway=gateway,
    )

    return Worker(
        queue=queue,
        supervisor=supervisor,
        worker_id=f"worker_{os.getpid()}",
        reconcilers=reconcilers,
    )


def build_tool_gateway(settings, store, approval_store, email_provider, crm_provider):
    """Stage B Phase 2 (P0-1): build a ToolGateway wired to the email + CRM
    providers via a provider_router.

    The gateway enforces the 7-check policy layer (approval verify-and-consume,
    payload hash binding, scope, rate limit, idempotency, audit) at the actual
    send/sync site. The SendingSpecialist calls gateway.execute("send_email", ...)
    with the REAL run.pending_approval_id; the CRMUpdatingSpecialist calls
    gateway.execute("sync_lead_to_crm", ...).

    The provider_router maps tool_name -> async callable(args, ctx) -> result,
    so the gateway dispatches directly to the provider (no MCP roundtrip) while
    still running every policy check. The MCPClient is an empty stub — it is
    only used if a tool NOT in the router is called (which raises
    ToolNotFoundError, the correct fail-closed behavior).

    Returns the ToolGateway (or None if no providers are configured, so
    dev/test falls back to the direct-provider path).
    """
    from opsmesh.mcp.base import MCPClient
    from opsmesh.approval_store import RealApprovalVerifier
    from opsmesh.security_context import InMemoryAuditLogger, InMemoryRateLimiter
    from opsmesh.tool_gateway import ToolGateway

    # Build the provider_router: each entry is async (args: dict, ctx) -> result.
    router = {}

    if email_provider is not None:
        async def _send_email_router(args, ctx):
            from opsmesh.adapters.email.base import EmailMessage
            msg = EmailMessage(
                to=args["to"],
                from_=args["from_"],
                subject=args["subject"],
                body=args["body"],
                approval_id=args.get("approval_id", ""),
                idempotency_key=f"idem_{ctx.run_id}",
            )
            return await email_provider.send(msg)
        router["send_email"] = _send_email_router

    if crm_provider is not None:
        async def _sync_lead_router(args, ctx):
            from opsmesh.adapters.crm.base import CRMLeadCreate
            return await crm_provider.create_lead(CRMLeadCreate(
                workspace_id=ctx.workspace_id,
                company=args.get("company", ""),
                contact_email=args.get("contact_email", ""),
                contact_name=args.get("contact_name") or None,
                industry=args.get("industry") or None,
            ))
        router["sync_lead_to_crm"] = _sync_lead_router

    if not router:
        # No providers — return None so specialists use the dev/test direct path.
        return None

    verifier = RealApprovalVerifier(store=approval_store)
    # Stage B Phase 4: use the durable SQLiteAuditLogger when a DB is
    # configured, so the audit trail survives restarts. In-memory logger
    # for dev/test.
    from opsmesh.adapters.db.connection import get_db_backend
    _backend = get_db_backend()
    if _backend in ("sqlite", "postgres"):
        from opsmesh.adapters.db.sqlite_activity_stores import SQLiteAuditLogger
        audit_logger = SQLiteAuditLogger()
    else:
        audit_logger = InMemoryAuditLogger()
    return ToolGateway(
        client=MCPClient(),  # empty stub — provider_router covers the specialist tools
        store=store,
        approval_verifier=verifier,
        audit_logger=audit_logger,
        rate_limiter=InMemoryRateLimiter(),
        provider_router=router,
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

    # Stage B Phase 2 (P0-1): build the ToolGateway wired to the email + CRM
    # providers. Specialists route side effects through it (approval
    # verify-and-consume at the send site). Returns None when no providers
    # are configured (dev/test mock mode) — specialists then use the direct
    # provider path.
    gateway = build_tool_gateway(
        settings=settings,
        store=store,
        approval_store=approval_gate.approval_store,
        email_provider=email,
        crm_provider=crm,
    )

    # Stage B Phase 3: build the CRM outbox store + the 3 reconcilers.
    # The outbox decouples CRM sync from the email send (Correction 2: CRM
    # failure never triggers email resend). The drainer claims + drains
    # pending ops with backoff. The other two reconcilers resolve SEND_UNKNOWN
    # runs + expire timed-out approvals.
    from opsmesh.reconciler.email_send import EmailSendReconciler
    from opsmesh.reconciler.approval_timeout import ApprovalTimeoutReconciler
    from opsmesh.reconciler.crm_outbox import CRMOutboxStore, CRMOutboxDrainer

    outbox_store = CRMOutboxStore()
    reconcilers: list = []
    if email is not None:
        reconcilers.append(EmailSendReconciler(
            store=store, email_provider=email,
            default_workspace_id=getattr(settings, "demo_workspace_id", "default"),
        ))
    reconcilers.append(ApprovalTimeoutReconciler(
        store=store, approval_store=approval_gate.approval_store,
    ))
    if crm is not None:
        reconcilers.append(CRMOutboxDrainer(crm_provider=crm, outbox_store=outbox_store))

    specialists = build_specialists(
        llm=llm,
        settings=settings,
        research=research,
        email_provider=email,
        crm_provider=crm,
        lead_store=lead_store,
        sent_email_store=sent_email_store,
        history_store=history_store,
        gateway=gateway,
        outbox_store=outbox_store,
    )

    sup = Supervisor(
        specialists=specialists,
        store=store,
        approvals=approval_gate,
        gateway=gateway,
    )
    # Attach the reconcilers + outbox so the caller (app.py / _main_async) can
    # pass them to the Worker + expose them via /worker/status.
    sup.reconcilers = reconcilers
    sup.outbox_store = outbox_store
    return sup


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


__all__ = ["Worker", "POLL_INTERVAL_SECONDS", "build_in_process_supervisor", "build_tool_gateway"]
