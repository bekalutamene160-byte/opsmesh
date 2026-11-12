"""
OPSMESH Config — centralized environment-variable-driven configuration.

Architecture (Engineering Contract §8):
  Every overrideable value lives here. The precedence is:
    1. Hardcoded invariants (refusal list — cannot be overridden)
    2. Environment variables (set at deploy time)
    3. Code defaults (the values in this file)

  Every override that changes a value in the Engineering Contract is
  recorded in the ADRs.

USAGE:
    from opsmesh.config import Settings
    settings = Settings()
    print(settings.max_tokens)  # 8000 (default) or override from env

  Or use the singleton:
    from opsmesh.config import get_settings
    settings = get_settings()
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Centralized OPSMESH configuration.

    All values are read from environment variables (case-insensitive).
    Defaults match the Engineering Contract (§1-2).

    To override: set the env var before starting the app:
        export OPSMESH_MAX_TOKENS_PRO=12000
        python -m opsmesh.api.app
    """

    model_config = SettingsConfigDict(
        env_prefix="OPSMESH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Engineering Contract §1: Timeouts ---
    llm_timeout: int = 30              # seconds (hard cap)
    tool_timeout: int = 10             # seconds (default per-tool)
    webhook_timeout: int = 5           # seconds (handler must return 202)
    api_timeout: int = 30              # seconds (user-facing API)
    approval_timeout_hours: int = 24   # hours (Gate 1 + Gate 2)
    job_lease_seconds: int = 300       # 5 minutes
    worker_shutdown_seconds: int = 30  # graceful shutdown

    # --- Database (Phase 1: SQLite; Phase 7: Postgres) ---
    # database_url takes precedence over database_path when set.
    # - For Postgres (production): postgresql+asyncpg://user:pass@host:port/dbname
    # - For SQLite (dev):           sqlite:///./opsmesh.db  (or set database_path)
    # When neither is set (and not in production_mode), falls back to in-memory.
    database_path: str = ""  # SQLite file path (empty = use database_url or in-memory)
    database_url: str = ""   # Postgres URL (Phase 7) — takes precedence over database_path
    production_mode: bool = False  # when True: fail closed if DB not configured

    # --- In-process worker (single-service deployments) ---
    # When the API and worker share a single process (e.g., a free-tier
    # Render web service with no separate worker + no Postgres), the worker
    # runs as a background asyncio task inside the API process. This shares
    # the in-memory store/queue/lead_store/approval_store so leads created
    # by the API are visible to the worker's specialists.
    #   "auto"  (default): enabled when store is in-memory (no DB configured)
    #   "true":  always enabled (even with a DB — useful for single-service prod)
    #   "false": always disabled (use a separate `python -m opsmesh.worker` process)
    worker_in_process: str = "auto"

    # --- Engineering Contract §2: Maximums ---
    max_steps: int = 15
    max_tokens_pro: int = 8000
    max_tokens_flash: int = 12000
    max_tool_calls: int = 12
    max_cost_usd: float = 0.05
    max_run_seconds: float = 60.0
    concurrency_local: int = 4
    concurrency_deployed: int = 20
    webhook_payload_max_kb: int = 64
    api_body_max_kb: int = 1024
    draft_max_words: int = 250

    # --- Providers ---
    # LLM provider: Groq default (GPT-OSS 120B on free tier, since Llama 3.3 70B
    # is now Enterprise-only as of Aug 2026). Mock for CI.
    llm_provider: str = "mock"        # mock | groq | gemini
    llm_api_key: str = ""
    llm_default_model: str = "openai/gpt-oss-120b"  # Groq's recommended free-tier model
    crm_provider: str = "mock"        # mock | hubspot
    crm_api_key: str = ""
    email_provider: str = "mock"      # mock | resend
    email_api_key: str = ""
    email_kill_switch: bool = True     # safety: true = block all real sends (flip to false in prod)
    research_provider: str = "mock"   # mock | exa
    exa_api_key: str = ""

    # --- Webhook ---
    webhook_secret: str = ""
    tally_webhook_secret: str = ""  # separate from the generic webhook secret

    # --- Session cookie signing ---
    session_secret: str = ""

    # --- Google OAuth (production; local dev uses mock auth) ---
    # Used to verify Google ID tokens issued by Google Identity Services.
    # When google_oauth_client_id is empty, auth falls back to dev mode
    # (no token required). Set this + session_secret in production.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""

    # NOTE: Supabase support was removed in Phase 6. Authentication is now
    # Google OAuth (production) or dev mode (no auth). The session_secret
    # is used to sign our own session JWTs (NOT Google's ID tokens).

    # --- Access mode (Stage B Phase 1/5) ---
    # single_user_demo (default): no authentication; the app opens directly to
    #   the dashboard. ALL unauthenticated access is bound server-side to a
    #   single `demo_workspace_id`. Email sends are restricted to
    #   `email_test_recipient`; CRM mutations are gated by `crm_demo_mode`.
    #   This is an explicitly UNAUTHENTICATED DEMO mode — NOT secure for
    #   multi-tenant production. Documented migration path: flip to
    #   `multi_user` to re-enable Google OAuth + per-user workspaces.
    # multi_user: requires Google OAuth + session JWT; per-user workspaces.
    access_mode: str = "single_user_demo"
    demo_workspace_id: str = "default"  # the single workspace bound in demo mode

    # --- Demo-mode safety guards (Stage B Phase 1) ---
    # email_test_recipient: in single_user_demo mode, SendingSpecialist asserts
    #   the recipient equals this address before any real send. Set to the
    #   Resend account's verified test recipient.
    email_test_recipient: str = ""
    # crm_demo_mode: governs live HubSpot mutations.
    #   read_only (default): writes a SKIPPED_DEMO outbox op; never calls HubSpot.
    #   disabled: no CRM step at all.
    #   live: calls the real CRM (NOT recommended for public demo).
    crm_demo_mode: str = "read_only"

    # --- Observability (ADR-006) ---
    obs_backend: str = "local"        # local | otlp | console
    otel_exporter_otlp_endpoint: str = ""

    # --- Workspace defaults (seed) ---
    workspace_owner_email: str = "owner@example.com"
    # Resend's pre-verified test sender — works with no custom domain.
    # Format: "Display Name <localpart@resend.dev>". Override with a verified
    # custom domain for production outreach.
    workspace_sender_identity: str = "OpsMesh <onboarding@resend.dev>"

    # --- CORS (Stage B Phase 1) ---
    # Comma-separated list of allowed origins. Empty → same-origin only
    # (the frontend is served at /ui on the same origin, so no CORS is needed
    # for the demo). Set to explicit origins for multi-origin deployments.
    # Wildcard "*" is rejected in single_user_demo mode for safety.
    cors_allowed_origins: str = ""


@lru_cache()
def get_settings() -> Settings:
    """Get the cached Settings singleton.

    Uses lru_cache so the env is read once at startup. Call
    get_settings.cache_clear() to re-read (e.g., in tests).
    """
    return Settings()


# ---------------------------------------------------------------------------
# Demo-mode helpers (Stage B Phase 1)
# ---------------------------------------------------------------------------


def is_demo_mode(settings: Optional[Settings] = None) -> bool:
    """True when OPSMESH_ACCESS_MODE == 'single_user_demo'.

    In demo mode, all unauthenticated access is bound server-side to a single
    `demo_workspace_id`; no browser-supplied workspace_id is ever trusted.
    """
    s = settings or get_settings()
    return (s.access_mode or "").strip().lower() == "single_user_demo"


def resolve_bound_workspace(settings: Optional[Settings] = None) -> str:
    """Resolve the workspace ID this deployment is bound to.

    In single_user_demo mode: returns settings.demo_workspace_id (the single
    server-bound workspace). Browser-supplied workspace_ids are NEVER trusted.

    In multi_user mode: raises RuntimeError — the auth layer (get_current_user)
    must derive the workspace from the session JWT per-request.

    This is the single enforcement point for P0-6 (unauthenticated endpoints
    hardcoded to workspace 'default'). Every public route handler reads the
    workspace from here (or from get_current_user in multi_user mode), never
    from the request body/query.
    """
    s = settings or get_settings()
    if is_demo_mode(s):
        return s.demo_workspace_id or "default"
    # multi_user mode: workspace resolution is the auth layer's job.
    raise RuntimeError(
        "multi_user access_mode requires per-request workspace resolution via "
        "get_current_user; resolve_bound_workspace must not be called directly."
    )


__all__ = [
    "Settings",
    "get_settings",
    "is_demo_mode",
    "resolve_bound_workspace",
    "validate_config",
]


def validate_config(settings: Optional[Settings] = None) -> list[str]:
    """Stage B Phase 8: validate the config for invalid combinations.

    Returns a list of human-readable warning strings (empty if the config
    is sound). Called at startup so misconfiguration is loud, not silent.

    Checks:
      - access_mode is one of the allowed values.
      - crm_demo_mode is one of the allowed values (only in demo mode).
      - production_mode + multi_user without OAuth secrets is flagged.
      - email_provider=resend + kill_switch=false but no email_test_recipient
        in demo mode is flagged (sends would be blocked at the specialist).
    """
    s = settings or get_settings()
    warnings: list[str] = []
    valid_access_modes = {"single_user_demo", "multi_user"}
    if (s.access_mode or "").strip().lower() not in valid_access_modes:
        warnings.append(
            f"OPSMESH_ACCESS_MODE={s.access_mode!r} is not one of {sorted(valid_access_modes)}."
        )
    if is_demo_mode(s):
        valid_crm_modes = {"read_only", "disabled", "live"}
        if (s.crm_demo_mode or "").strip().lower() not in valid_crm_modes:
            warnings.append(
                f"OPSMESH_CRM_DEMO_MODE={s.crm_demo_mode!r} is not one of {sorted(valid_crm_modes)}."
            )
        if (
            (s.email_provider or "").lower() == "resend"
            and not s.email_kill_switch
            and not s.email_test_recipient
        ):
            warnings.append(
                "OPSMESH_EMAIL_PROVIDER=resend with EMAIL_KILL_SWITCH=false but "
                "OPSMESH_EMAIL_TEST_RECIPIENT is empty — demo sends will be blocked."
            )
    else:
        # multi_user mode: OAuth + session secret required if production_mode.
        if s.production_mode:
            if not getattr(s, "google_oauth_client_id", ""):
                warnings.append(
                    "multi_user + production_mode requires OPSMESH_GOOGLE_OAUTH_CLIENT_ID."
                )
            if not getattr(s, "session_secret", "") or "change-me" in (getattr(s, "session_secret", "") or ""):
                warnings.append(
                    "multi_user + production_mode requires OPSMESH_SESSION_SECRET."
                )
    return warnings
