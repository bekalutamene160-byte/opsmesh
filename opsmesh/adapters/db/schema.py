"""
OPSMESH Database Schema — DDL for both SQLite and Postgres.

Phase 7 (this file): Two parallel schema definitions — one for SQLite
(types: TEXT for everything, INSERT OR IGNORE for idempotency) and one
for Postgres (uses JSONB for JSON columns, BIGSERIAL for audit_events,
ON CONFLICT DO NOTHING for idempotency).

Both schemas define the SAME 9 tables:
  1. runs              — workflow run state (one row per run checkpoint)
  2. jobs              — job queue (one row per enqueued job)
  3. approvals         — approval records (Gate 1 + Gate 2)
  4. idempotency       — idempotency keys (prevent duplicate tool calls)
  5. crm_outbox        — pending CRM sync operations
  6. leads             — lead records (Phase 7: persisted, not in-memory)
  7. audit_events      — audit trail
  8. evidence_items    — research evidence
  9. workspace_members — SaaS: which user belongs to which workspace
                       (Phase 7: minimal — just user_id, workspace_id, role)

The schema is idempotent — safe to run on every startup.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# SQLite schema (uses TEXT for JSON, INSERT OR IGNORE for upserts)
# ---------------------------------------------------------------------------


SCHEMA_SQLITE = """
-- 1. runs
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    parent_run_id TEXT,
    state TEXT NOT NULL,
    step_id INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    email_message_id TEXT,
    reconciliation_attempts INTEGER DEFAULT 0,
    budget_used_tokens INTEGER DEFAULT 0,
    budget_used_cost_usd REAL DEFAULT 0.0,
    budget_used_tool_calls INTEGER DEFAULT 0,
    draft_attempts INTEGER DEFAULT 0,
    critic_attempts INTEGER DEFAULT 0,
    last_error TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    terminal_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_workspace_state ON runs(workspace_id, state);
CREATE INDEX IF NOT EXISTS idx_runs_lead ON runs(lead_id);
CREATE INDEX IF NOT EXISTS idx_runs_updated ON runs(updated_at);

-- 2. jobs
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    worker_id TEXT,
    lease_expires_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 4,
    enqueued_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_lease ON jobs(status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id);

-- 3. approvals
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    decision TEXT,
    decision_feedback TEXT,
    decided_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_workspace_status ON approvals(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_approvals_expires ON approvals(expires_at);

-- 4. idempotency
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    step_id INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idempotency_run ON idempotency(run_id);

-- 5. crm_outbox
CREATE TABLE IF NOT EXISTS crm_outbox (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    args TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status_next ON crm_outbox(status, next_attempt_at);

-- 6. leads
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    crm_lead_id TEXT,
    company TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    contact_name TEXT,
    industry TEXT,
    employees INTEGER,
    website TEXT,
    source TEXT NOT NULL,
    source_submission_id TEXT,
    disposition TEXT NOT NULL DEFAULT 'new',
    disposition_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id, source_submission_id)
);
CREATE INDEX IF NOT EXISTS idx_leads_workspace_status ON leads(workspace_id, disposition);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);

-- 7. audit_events
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    run_id TEXT,
    lead_id TEXT,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,
    span_id TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_workspace_time ON audit_events(workspace_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id);

-- 8. evidence_items
CREATE TABLE IF NOT EXISTS evidence_items (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    run_id TEXT,
    source TEXT NOT NULL,
    source_url TEXT,
    content TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'medium',
    is_material INTEGER NOT NULL DEFAULT 0,
    retrieved_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_lead ON evidence_items(lead_id);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence_items(run_id);

-- 9. workspace_members (Phase 7 — minimal SaaS membership)
CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'owner',
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_wm_user ON workspace_members(user_id);
"""


# ---------------------------------------------------------------------------
# Postgres schema (uses JSONB for JSON, BIGSERIAL for audit_events,
# ON CONFLICT DO NOTHING for idempotent upserts)
# ---------------------------------------------------------------------------


SCHEMA_POSTGRES = [
    # 1. runs — JSONB payload for fast jsonb_path_query
    """
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        lead_id TEXT NOT NULL,
        parent_run_id TEXT,
        state TEXT NOT NULL,
        step_id INTEGER NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 0,
        payload JSONB NOT NULL,
        email_message_id TEXT,
        reconciliation_attempts INTEGER DEFAULT 0,
        budget_used_tokens INTEGER DEFAULT 0,
        budget_used_cost_usd DOUBLE PRECISION DEFAULT 0.0,
        budget_used_tool_calls INTEGER DEFAULT 0,
        draft_attempts INTEGER DEFAULT 0,
        critic_attempts INTEGER DEFAULT 0,
        last_error TEXT,
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        terminal_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_workspace_state ON runs(workspace_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_runs_lead ON runs(lead_id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_updated ON runs(updated_at)",

    # 2. jobs
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        worker_id TEXT,
        lease_expires_at TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 4,
        enqueued_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        last_error TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status_lease ON jobs(status, lease_expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id)",

    # 3. approvals
    """
    CREATE TABLE IF NOT EXISTS approvals (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        action TEXT NOT NULL,
        payload JSONB NOT NULL,
        payload_hash TEXT NOT NULL,
        decision TEXT,
        decision_feedback TEXT,
        decided_by TEXT,
        decided_at TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_approvals_workspace_status ON approvals(workspace_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_approvals_expires ON approvals(expires_at)",

    # 4. idempotency
    """
    CREATE TABLE IF NOT EXISTS idempotency (
        key TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        step_id INTEGER NOT NULL,
        tool_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        result JSONB,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_idempotency_run ON idempotency(run_id)",

    # 5. crm_outbox
    """
    CREATE TABLE IF NOT EXISTS crm_outbox (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        lead_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        args JSONB NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 5,
        next_attempt_at TEXT NOT NULL,
        last_error TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_outbox_status_next ON crm_outbox(status, next_attempt_at)",

    # 6. leads — with UNIQUE constraint on (workspace_id, source_submission_id)
    """
    CREATE TABLE IF NOT EXISTS leads (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        crm_lead_id TEXT,
        company TEXT NOT NULL,
        contact_email TEXT NOT NULL,
        contact_name TEXT,
        industry TEXT,
        employees INTEGER,
        website TEXT,
        source TEXT NOT NULL,
        source_submission_id TEXT,
        disposition TEXT NOT NULL DEFAULT 'new',
        disposition_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(workspace_id, source_submission_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_leads_workspace_status ON leads(workspace_id, disposition)",
    "CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at)",

    # 7. audit_events — BIGSERIAL for high-volume
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        id BIGSERIAL PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        run_id TEXT,
        lead_id TEXT,
        kind TEXT NOT NULL,
        actor TEXT NOT NULL,
        detail JSONB NOT NULL,
        span_id TEXT,
        occurred_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_workspace_time ON audit_events(workspace_id, occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id)",

    # 8. evidence_items
    """
    CREATE TABLE IF NOT EXISTS evidence_items (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        lead_id TEXT NOT NULL,
        run_id TEXT,
        source TEXT NOT NULL,
        source_url TEXT,
        content TEXT NOT NULL,
        confidence TEXT NOT NULL DEFAULT 'medium',
        is_material BOOLEAN NOT NULL DEFAULT FALSE,
        retrieved_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_evidence_lead ON evidence_items(lead_id)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence_items(run_id)",

    # 9. workspace_members (Phase 7 — minimal SaaS membership)
    """
    CREATE TABLE IF NOT EXISTS workspace_members (
        workspace_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        email TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'owner',
        created_at TEXT NOT NULL,
        PRIMARY KEY (workspace_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wm_user ON workspace_members(user_id)",
]


__all__ = ["SCHEMA_SQLITE", "SCHEMA_POSTGRES"]
