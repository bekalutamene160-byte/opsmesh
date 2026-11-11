"""
OPSMESH Initial Database Schema — 8 tables.

Creates the complete schema for SQLite. All tables have workspace_id for
isolation. Uses TEXT for timestamps (ISO 8601 UTC) and TEXT for JSON
payloads (SQLite doesn't have JSONB, but json_extract() works on TEXT).

Run: alembic upgrade head
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create all 8 tables + indexes."""

    # --- 1. runs ---
    op.create_table(
        "runs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("lead_id", sa.Text, nullable=False),
        sa.Column("parent_run_id", sa.Text, nullable=True),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("step_id", sa.Integer, nullable=False, server_default="0"),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("payload", sa.Text, nullable=False),  # JSON: full RunRecord
        sa.Column("email_message_id", sa.Text, nullable=True),
        sa.Column("reconciliation_attempts", sa.Integer, server_default="0"),
        sa.Column("budget_used_tokens", sa.Integer, server_default="0"),
        sa.Column("budget_used_cost_usd", sa.Float, server_default="0.0"),
        sa.Column("budget_used_tool_calls", sa.Integer, server_default="0"),
        sa.Column("draft_attempts", sa.Integer, server_default="0"),
        sa.Column("critic_attempts", sa.Integer, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.Column("terminal_at", sa.Text, nullable=True),
    )
    op.create_index("idx_runs_workspace_state", "runs", ["workspace_id", "state"])
    op.create_index("idx_runs_lead", "runs", ["lead_id"])
    op.create_index("idx_runs_updated", "runs", ["updated_at"])

    # --- 2. jobs ---
    op.create_table(
        "jobs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("worker_id", sa.Text, nullable=True),
        sa.Column("lease_expires_at", sa.Text, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="4"),
        sa.Column("enqueued_at", sa.Text, nullable=False),
        sa.Column("started_at", sa.Text, nullable=True),
        sa.Column("completed_at", sa.Text, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
    )
    op.create_index("idx_jobs_status_lease", "jobs", ["status", "lease_expires_at"])
    op.create_index("idx_jobs_run", "jobs", ["run_id"])

    # --- 3. approvals ---
    op.create_table(
        "approvals",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),  # JSON
        sa.Column("payload_hash", sa.Text, nullable=False),
        sa.Column("decision", sa.Text, nullable=True),
        sa.Column("decision_feedback", sa.Text, nullable=True),
        sa.Column("decided_by", sa.Text, nullable=True),
        sa.Column("decided_at", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("expires_at", sa.Text, nullable=False),
    )
    op.create_index("idx_approvals_workspace_status", "approvals", ["workspace_id", "status"])
    op.create_index("idx_approvals_expires", "approvals", ["expires_at"])

    # --- 4. idempotency ---
    op.create_table(
        "idempotency",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("step_id", sa.Integer, nullable=False),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("result", sa.Text, nullable=True),  # JSON
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )
    op.create_index("idx_idempotency_run", "idempotency", ["run_id"])

    # --- 5. crm_outbox ---
    op.create_table(
        "crm_outbox",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("lead_id", sa.Text, nullable=False),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("args", sa.Text, nullable=False),  # JSON
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.Text, nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("completed_at", sa.Text, nullable=True),
    )
    op.create_index("idx_outbox_status_next", "crm_outbox", ["status", "next_attempt_at"])

    # --- 6. leads ---
    op.create_table(
        "leads",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("crm_lead_id", sa.Text, nullable=True),
        sa.Column("company", sa.Text, nullable=False),
        sa.Column("contact_email", sa.Text, nullable=False),
        sa.Column("contact_name", sa.Text, nullable=True),
        sa.Column("industry", sa.Text, nullable=True),
        sa.Column("employees", sa.Integer, nullable=True),
        sa.Column("website", sa.Text, nullable=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("source_submission_id", sa.Text, nullable=True),
        sa.Column("disposition", sa.Text, nullable=False, server_default="new"),
        sa.Column("disposition_reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.UniqueConstraint("workspace_id", "source_submission_id",
                           name="uq_leads_workspace_submission"),
    )
    op.create_index("idx_leads_workspace_status", "leads", ["workspace_id", "disposition"])

    # --- 7. audit_events ---
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("run_id", sa.Text, nullable=True),
        sa.Column("lead_id", sa.Text, nullable=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("detail", sa.Text, nullable=False),  # JSON (redacted)
        sa.Column("span_id", sa.Text, nullable=True),
        sa.Column("occurred_at", sa.Text, nullable=False),
    )
    op.create_index("idx_audit_workspace_time", "audit_events", ["workspace_id", "occurred_at"])
    op.create_index("idx_audit_run", "audit_events", ["run_id"])

    # --- 8. evidence_items ---
    op.create_table(
        "evidence_items",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("workspace_id", sa.Text, nullable=False),
        sa.Column("lead_id", sa.Text, nullable=False),
        sa.Column("run_id", sa.Text, nullable=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("source_url", sa.Text, nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("confidence", sa.Text, nullable=False, server_default="medium"),
        sa.Column("is_material", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("retrieved_at", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index("idx_evidence_lead", "evidence_items", ["lead_id"])
    op.create_index("idx_evidence_run", "evidence_items", ["run_id"])


def downgrade() -> None:
    """Drop all tables (dev only — never run in production)."""
    op.drop_table("evidence_items")
    op.drop_table("audit_events")
    op.drop_table("leads")
    op.drop_table("crm_outbox")
    op.drop_table("idempotency")
    op.drop_table("approvals")
    op.drop_table("jobs")
    op.drop_table("runs")
