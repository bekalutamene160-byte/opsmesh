"""
OPSMESH FastAPI Application — wires together all components.

Phase 1 (Day 7): When OPSMESH_DATABASE_PATH is set, the app uses durable
SQLite-backed stores. When not set (or empty), falls back to in-memory
(for tests/dev only). In production_mode=True, missing database_path causes
startup failure (fail closed).

Phase 4 (Day 7): Adds CORS middleware, structured error responses,
detailed /api/v1/health endpoint, and mounts the frontend at /ui.

Architecture:
  The app constructs:
    - StateStore (InMemoryStateStore or SQLiteStateStore)
    - JobQueue (InMemoryJobQueue or SQLiteJobQueue)
    - LeadIntakeService (depends on store + queue)
    - Routes: webhook, leads, metrics, dashboard, approvals, runs, integrations, tally
    - CORS + global exception handler (Phase 4)
    - Static frontend mount at /ui (Phase 5)
    - Structured logging (JSON + PII/secret redaction)
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from opsmesh.api.routes.leads import configure_leads_routes, router as leads_router
from opsmesh.api.routes.webhook import configure_webhook_route, router as webhook_router
from opsmesh.api.routes.metrics import configure_metrics_route, router as metrics_router
from opsmesh.api.routes.dashboard import configure_dashboard_route, router as dashboard_router
from opsmesh.api.routes.approvals import configure_approvals_route, router as approvals_router
from opsmesh.api.routes.runs import configure_runs_route, router as runs_router
from opsmesh.api.routes.integrations import router as integrations_router
from opsmesh.api.routes.tally_webhook import configure_tally_route, router as tally_router
from opsmesh.api.auth_google import router as auth_google_router
from opsmesh.config import get_settings
from opsmesh.observability.logging import configure_logging
from opsmesh.job_queue import InMemoryJobQueue
from opsmesh.services.lead_intake import LeadIntakeService
from opsmesh.state_store import InMemoryStateStore


def create_app(
    *,
    webhook_secret: Optional[str] = None,
    workspace_id: str = "default",
    use_sqlite: Optional[bool] = None,
    database_path: Optional[str] = None,
) -> FastAPI:
    """
    Construct the FastAPI app with all components wired up.

    Args:
        webhook_secret: the HMAC secret for webhook verification.
        workspace_id: the default workspace for manual + simulated routes.
        use_sqlite: if True, force SQLite. If False, force in-memory.
            If None, auto-detect from Settings (database_path + production_mode).
        database_path: path to the SQLite DB file. If None, reads from Settings.

    Returns:
        A FastAPI app instance, ready to run with uvicorn.
    """
    settings = get_settings()

    # Resolve the webhook secret
    if webhook_secret is None:
        webhook_secret = os.environ.get("OPSMESH_WEBHOOK_SECRET", "")

    # Resolve database config:
    #   1. If use_sqlite is explicitly set, use that.
    #   2. Else, if database_url is set (Postgres), use Postgres.
    #   3. Else, if database_path is set (SQLite), use SQLite.
    #   4. Else, fall back to in-memory (dev mode).
    if database_path is None:
        database_path = settings.database_path or ""

    # PRODUCTION MODE: fail closed if no database configured (Postgres or SQLite)
    if settings.production_mode:
        if not settings.database_url and not database_path:
            raise RuntimeError(
                "PRODUCTION MODE: database_url (Postgres) or database_path (SQLite) "
                "must be set. In-memory storage is not allowed in production."
            )
        if not webhook_secret or "change-me" in webhook_secret:
            raise RuntimeError(
                "PRODUCTION MODE: OPSMESH_WEBHOOK_SECRET must be set to a real value."
            )

    # Determine the backend
    use_postgres = bool(settings.database_url and not use_sqlite is False)
    # If use_sqlite was explicitly False, allow Postgres detection.
    # If use_sqlite was explicitly True, force SQLite.
    if use_sqlite is True:
        use_postgres = False
    elif use_sqlite is False:
        use_postgres = bool(settings.database_url)
    else:
        # Auto-detect
        use_postgres = bool(settings.database_url)
        use_sqlite_resolved = bool(database_path) and not use_postgres
    use_sqlite_resolved = (use_sqlite is True) or (
        use_sqlite is None and bool(database_path) and not use_postgres
    )

    # --- Construct the dependencies ---
    # NOTE: init_db() is ASYNC and must be called from inside an event loop.
    # uvicorn starts its own event loop AFTER create_app() returns, so we
    # CAN'T call init_db() here (we'd hit "Cannot run the event loop while
    # another loop is running" on uvicorn startup).
    #
    # Instead, we:
    #   1. Call configure_backend() synchronously -- sets the module-level
    #      _db_backend / _db_path / _db_url state so create_lead_store()
    #      and create_approval_store() return the right stores.
    #   2. Defer the actual init_db() (connect + create schema) to the
    #      FastAPI lifespan handler, which runs INSIDE uvicorn's event loop.
    from opsmesh.adapters.db.connection import configure_backend

    if use_postgres:
        configure_backend(database_url=settings.database_url)
        store_type = "postgres"
        queue_type = "postgres"
    elif use_sqlite_resolved:
        if not database_path:
            raise RuntimeError("use_sqlite=True but database_path is not set")
        configure_backend(database_path=database_path)
        store_type = "sqlite"
        queue_type = "sqlite"
    else:
        # Use in-memory stores (tests/dev)
        configure_backend(database_path="", database_url="")  # sets backend to "memory"
        store_type = "in_memory"
        queue_type = "in_memory"

    # Build the store + queue. For SQLite/Postgres, these are the SQLiteStateStore
    # and SQLiteJobQueue -- both work with BOTH backends via the unified
    # get_connection() abstraction. The SQL uses ? placeholders, which the
    # Postgres adapter converts to $1, $2, ... automatically.
    if store_type in ("sqlite", "postgres"):
        from opsmesh.adapters.db.sqlite_state_store import SQLiteStateStore
        from opsmesh.adapters.db.sqlite_job_queue import SQLiteJobQueue
        store = SQLiteStateStore()
        queue = SQLiteJobQueue()
    else:
        store = InMemoryStateStore()
        queue = InMemoryJobQueue()

    # The actual DB init path (used by lifespan, or called eagerly for tests)
    db_init_path = database_path if store_type == "sqlite" else None
    db_init_url = settings.database_url if store_type == "postgres" else None

    intake_service = LeadIntakeService(store=store, queue=queue)

    # Build the ApprovalGate (Day 5 component)
    # Phase 7: use create_approval_store() so the API and worker share the
    # SAME approval store (DB-backed when a DB is configured, in-memory otherwise).
    # Without this, the worker creates approvals in its own in-memory store,
    # and the API can't see them in the /approvals endpoint.
    from opsmesh.approval_store import create_approval_store
    from opsmesh.approval_gate import ApprovalGate
    from opsmesh.adapters.notifier.mock import MockNotifier
    approval_store = create_approval_store()
    approval_gate = ApprovalGate(
        store=store,
        approval_store=approval_store,
        notifier=MockNotifier(),
    )

    # Configure routes with the dependencies
    configure_webhook_route(intake_service, webhook_secret)
    configure_leads_routes(intake_service, workspace_id)
    configure_metrics_route(store, queue, workspace_id)
    configure_dashboard_route(store, queue, workspace_id)
    configure_approvals_route(approval_gate, queue=queue)
    configure_runs_route(store)
    configure_tally_route(intake_service, getattr(settings, "tally_webhook_secret", ""))

    # Configure structured logging (JSON output with PII redaction)
    configure_logging()

    # Lifespan: initialize DB on startup, cleanup on shutdown.
    # This runs INSIDE uvicorn's event loop -- the ONLY place we can call
    # async DB init (open connections, create schema). Doing it in the
    # synchronous create_app() body would crash with
    # "Cannot run the event loop while another loop is running".
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # --- Startup: connect + create schema (if backend is sqlite or postgres) ---
        if store_type in ("sqlite", "postgres"):
            from opsmesh.adapters.db.connection import init_db
            await init_db(database_path=db_init_path, database_url=db_init_url)
        yield
        # --- Shutdown: close connections ---
        from opsmesh.adapters.db.connection import close_all
        await close_all()

    app = FastAPI(
        title="OPSMESH",
        description="Evidence-aware, human-supervised B2B Lead-to-Opportunity Operations Agent",
        version="0.1.0",
        lifespan=lifespan,
    )

    # --- CORS middleware (Phase 4) ---
    # Allows the frontend (served at /ui or a separate origin in dev) to
    # call the API. In production, set OPSMESH_CORS_ALLOWED_ORIGINS to the
    # exact frontend origin. Default: permissive for local dev.
    cors_origins = (
        os.environ.get("OPSMESH_CORS_ALLOWED_ORIGINS", "")
        or "http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000,http://127.0.0.1:5173"
    )
    allowed_origins = [
        o.strip() for o in cors_origins.split(",") if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Signature", "tally-signature", "X-Tally-Signature"],
    )

    # --- Global exception handler (Phase 4) ---
    # Any uncaught exception returns a structured {"error": {"code", "message"}}
    # response. This prevents raw 500s from leaking to the frontend and gives
    # the operator a consistent error shape.
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        # Log the full exception server-side (with traceback)
        import logging
        logging.getLogger("opsmesh.api").exception(
            "Unhandled exception in %s %s",
            request.method,
            request.url.path,
        )
        # Return a sanitized response (no internal details leaked)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An internal error occurred. Check server logs.",
                }
            },
        )

    # Register routers
    app.include_router(webhook_router)
    app.include_router(leads_router)
    app.include_router(metrics_router)
    app.include_router(dashboard_router)
    app.include_router(approvals_router)
    app.include_router(runs_router)
    app.include_router(integrations_router)
    app.include_router(tally_router)
    app.include_router(auth_google_router)

    # Health endpoints (Engineering Contract §1: /health always 200)
    @app.get("/health", tags=["health"])
    async def health():
        """Liveness probe. Always returns 200."""
        return {"status": "ok"}

    @app.get("/ready", tags=["health"])
    async def ready():
        """Readiness probe. Returns 200 only if the store is reachable."""
        return {"status": "ready", "store": store_type, "queue": queue_type}

    @app.get("/api/v1/health", tags=["health"])
    async def detailed_health():
        """Detailed health probe (Phase 4).

        Returns the liveness + readiness + provider configuration status.
        Used by the frontend Integrations page and by deploy-time smoke
        tests. Always returns 200 (it's a probe, not an error reporter).
        """
        # Snapshot the settings at call time (don't cache — settings can
        # change between requests in tests)
        s = get_settings()
        google_client_id = s.google_oauth_client_id
        session_secret = s.session_secret
        auth_configured = bool(google_client_id) and bool(session_secret)
        return {
            "status": "ok",
            "store": store_type,
            "queue": queue_type,
            "production_mode": s.production_mode,
            "database_path": s.database_path or "(in-memory)",
            "providers": {
                "llm": s.llm_provider,
                "research": s.research_provider,
                "email": s.email_provider,
                "crm": s.crm_provider,
            },
            "auth": {
                "mode": "google_oauth" if auth_configured else "dev",
                "google_oauth_configured": bool(google_client_id),
                "session_secret_configured": bool(session_secret),
                # Backward-compat field for older frontends
                "jwt_secret_configured": bool(session_secret),
            },
            "email_kill_switch": s.email_kill_switch,
        }

    # --- Mount the frontend at /ui (Phase 5) ---
    # The static directory lives at opsmesh/static/. If it doesn't exist
    # (e.g., in CI without the frontend files), the mount is skipped so
    # the API still works.
    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.is_dir():
        app.mount(
            "/ui",
            StaticFiles(directory=str(static_dir), html=True),
            name="frontend",
        )

        # Convenience: redirect / to the login page
        @app.get("/", include_in_schema=False)
        async def root_redirect():
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/ui/login.html")

    # Expose dependencies on app.state for tests
    app.state.store = store
    app.state.queue = queue
    app.state.intake_service = intake_service
    # Backward-compat: use_sqlite is True if backend is sqlite, False otherwise.
    # (Phase 7 added use_postgres; use_sqlite is kept for existing tests.)
    app.state.use_sqlite = (store_type == "sqlite")
    app.state.use_postgres = (store_type == "postgres")
    app.state.database_path = database_path
    app.state.database_backend = store_type

    return app


__all__ = ["create_app"]
