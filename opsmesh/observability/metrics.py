"""
OPSMESH Metrics — the 8 minimum-viable metrics for the ops dashboard.

Architecture (course Ch 17 + §12):
  Every metric on the dashboard answers ONE question an on-call engineer
  would ask. If a metric doesn't answer an actionable question, remove it.

  The 8 metrics:
    1. runs.total_24h         — Are we getting traffic?
    2. runs.success_rate      — Is the system healthy?
    3. queue.depth            — Are we keeping up?
    4. runs.p95_latency_ms    — Are we meeting SLO?
    5. cost.total_usd_24h     — Are we burning budget?
    6. queue.dlq_depth        — Permanent failures needing attention?
    7. approvals.pending_count — Humans blocking the workflow?
    8. providers.llm.error_rate — Provider health?

  Metrics are computed ON-DEMAND from the StateStore (no separate metrics
  store in v1). For larger scale (10k+ runs), a materialized view or
  separate metrics table would be needed.

USAGE:
    from opsmesh.observability.metrics import compute_metrics
    metrics = await compute_metrics(store=state_store, workspace_id="default")
    # Returns a dict with all 8 metrics
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from opsmesh.models import utcnow
from opsmesh.state_machine import State


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------


async def compute_metrics(
    store: Any,  # StateStore (typed loosely to avoid circular imports)
    workspace_id: str = "default",
    queue: Any = None,  # Optional[JobQueueProtocol]
) -> dict[str, Any]:
    """
    Compute the 8 minimum-viable metrics from the StateStore + JobQueue.

    Iterates all 16 states to collect all runs in the workspace, then
    computes the metrics. For Day 6 (in-memory store), this is fast.
    For production (Postgres), Day 7 will add a single-query optimization.

    Args:
        store: the StateStore (InMemoryStateStore or PostgresStateStore).
        workspace_id: the workspace to compute metrics for.
        queue: optional JobQueue for queue.depth and queue.dlq_depth.

    Returns:
        A dict with all 8 metrics + metadata.
    """
    # Collect all runs in the workspace (iterate all 16 states)
    all_runs = []
    for state in State:
        runs = await store.list_in_state(workspace_id, state.value, limit=10000)
        all_runs.extend(runs)

    # Compute time window (last 24 hours)
    now = utcnow()
    cutoff = now - timedelta(hours=24)

    # Filter to runs updated in the last 24h
    recent_runs = [r for r in all_runs if r.updated_at > cutoff]

    # --- Metric 1: runs.total_24h ---
    total_24h = len(recent_runs)

    # --- Metric 2: runs.success_rate ---
    # Success = runs that reached SENT (terminal, email confirmed)
    succeeded = sum(1 for r in recent_runs if r.state == State.SENT)
    failed = sum(
        1 for r in recent_runs
        if r.state in (State.FAILED, State.REJECTED, State.STOPPED_BY_USER,
                       State.APPROVAL_EXPIRED)
    )
    success_rate = succeeded / total_24h if total_24h > 0 else 0.0

    # --- Metric 3: queue.depth ---
    queue_depth = 0
    if queue is not None:
        queue_depth = await queue.depth(workspace_id)

    # --- Metric 4: runs.p95_latency_ms ---
    # FIX: Only count TERMINAL runs in latency calculations. Paused or active
    # runs are not "completed" — including them would skew the latency.
    latencies = []
    for r in recent_runs:
        if not r.is_terminal():
            continue  # skip non-terminal runs
        end = r.terminal_at or r.updated_at
        if r.started_at and end:
            latency_ms = (end - r.started_at).total_seconds() * 1000
            latencies.append(latency_ms)
    p95_latency_ms = _percentile(latencies, 95) if latencies else 0

    # --- Metric 5: cost.total_usd_24h ---
    # FIX: Only count cost from runs that actually consumed tokens (budget_used_tokens > 0).
    # Do not invent costs — only report what was actually consumed.
    total_cost = sum(
        r.budget_used_cost_usd for r in recent_runs
        if r.budget_used_tokens > 0  # only runs that consumed tokens
    )

    # --- Metric 6: queue.dlq_depth ---
    dlq_depth = 0
    if queue is not None:
        dlq_depth = await queue.dlq_depth(workspace_id)

    # --- Metric 7: approvals.pending_count ---
    pending_approvals = sum(
        1 for r in all_runs
        if r.state in (State.AWAITING_APPROVAL, State.AWAITING_EVIDENCE_REVIEW)
    )

    # --- Metric 8: providers.llm.error_rate ---
    # FIX: The denominator must be LLM CALL COUNT, not tool_call_count.
    # budget_used_tokens tracks tokens consumed (which come from LLM calls).
    # budget_used_tool_calls tracks ALL tool calls (including CRM lookups,
    # web_search, etc.) — using it as the denominator for LLM error rate is
    # mathematically wrong.
    #
    # Correct denominator: number of runs that made LLM calls (budget_used_tokens > 0).
    # This is an approximation — the ideal is a dedicated llm_call_count field,
    # but for Day 6 we use budget_used_tokens > 0 as a proxy for "this run made
    # at least one LLM call."
    llm_errors = sum(
        1 for r in recent_runs
        if r.last_error and any(
            err in r.last_error
            for err in ("TimeoutError", "ConnectionError", "LLMProviderError", "RetryExhausted")
        )
    )
    # FIX: use token-consuming runs as denominator, not tool calls
    llm_call_count = sum(
        1 for r in recent_runs if r.budget_used_tokens > 0
    )
    llm_error_rate = llm_errors / llm_call_count if llm_call_count > 0 else 0.0

    # Build the metrics dict
    return {
        "generated_at": now.isoformat(),
        "window": "24h",
        "workspace_id": workspace_id,
        "runs": {
            "total_24h": total_24h,
            "succeeded_24h": succeeded,
            "failed_24h": failed,
            "success_rate": round(success_rate, 4),
            "p95_latency_ms": round(p95_latency_ms, 2),
            "p50_latency_ms": round(_percentile(latencies, 50) if latencies else 0, 2),
        },
        "queue": {
            "depth": queue_depth,
            "dlq_depth": dlq_depth,
        },
        "cost": {
            "total_usd_24h": round(total_cost, 6),
        },
        "approvals": {
            "pending_count": pending_approvals,
        },
        "providers": {
            "llm": {
                "error_rate": round(llm_error_rate, 4),
                "calls_24h": llm_call_count,
                "errors_24h": llm_errors,
            },
        },
    }


def _percentile(data: list[float], p: float) -> float:
    """Compute the p-th percentile of a list of floats.

    Uses the "nearest rank" method (simple, no interpolation).
    """
    if not data:
        return 0.0
    sorted_data = sorted(data)
    n = len(sorted_data)
    rank = max(1, int(round(p / 100 * n)))
    return sorted_data[min(rank - 1, n - 1)]


__all__ = ["compute_metrics"]
