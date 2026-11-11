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

    # --- Observability (ADR-006) ---
    obs_backend: str = "local"        # local | otlp | console
    otel_exporter_otlp_endpoint: str = ""

    # --- Workspace defaults (seed) ---
    workspace_owner_email: str = "owner@example.com"
    workspace_sender_identity: str = "Bekalu <bekalu@opsmesh.dev>"


@lru_cache()
def get_settings() -> Settings:
    """Get the cached Settings singleton.

    Uses lru_cache so the env is read once at startup. Call
    get_settings.cache_clear() to re-read (e.g., in tests).
    """
    return Settings()


__all__ = ["Settings", "get_settings"]
