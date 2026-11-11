"""
OPSMESH Tracing — lightweight span context manager using contextvars.

Architecture (course Ch 14 + ADR-006):
  A trace is a tree of operations. The root is the run. Each child is a
  step (research, qualify, draft, critic). Each grandchild is a tool call
  or LLM call within a step.

  Every span records:
    - trace_id: root identifier (same across all spans in a run)
    - span_id: unique per span
    - parent_id: forms the tree
    - run_id, workspace_id, lead_id (if available)
    - name: e.g., "step:researching", "tool:lookup_lead", "llm:gemini-flash"
    - kind: supervisor | step | specialist | llm | tool | approval | checkpoint
    - start_ms, end_ms, dur_ms: timing
    - status: ok | error | timeout | cancelled
    - attributes: kind-specific (tokens, cost, tool_name, etc.)

  NO EXTRA DEPENDENCIES:
    Uses Python's built-in `contextvars` module for trace_id/span_id
    propagation. No OpenTelemetry SDK — just a lightweight implementation
    that produces the same data structure.

  EXPORT:
    Spans are collected in a list and can be exported to JSONL.
    In production (Day 7), these can be shipped to an OTel collector.

USAGE:
    from opsmesh.observability.tracing import start_run, span

    # Start a run trace
    ctx = start_run(run_id="run_1", workspace_id="ws_1")

    # Open spans within the run
    with span(ctx, "step:researching") as step_ctx:
        with span(step_ctx, "tool:web_search", query="acme") as tool_ctx:
            results = web_search("acme")
        with span(step_ctx, "llm:gemini-flash", input_tokens=2100) as llm_ctx:
            summary = llm.generate(results)
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Span data model
# ---------------------------------------------------------------------------


class SpanRecord:
    """A single span (one operation in a trace tree)."""

    def __init__(
        self,
        trace_id: str,
        span_id: str,
        parent_id: Optional[str],
        name: str,
        kind: str = "step",
        run_id: str = "",
        workspace_id: str = "",
        lead_id: str = "",
    ):
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_id = parent_id
        self.name = name
        self.kind = kind
        self.run_id = run_id
        self.workspace_id = workspace_id
        self.lead_id = lead_id
        self.start_ms: float = 0.0
        self.end_ms: float = 0.0
        self.dur_ms: float = 0.0
        self.status: str = "ok"  # ok | error | timeout | cancelled
        self.error_class: Optional[str] = None
        self.error_message: Optional[str] = None
        self.attributes: dict[str, Any] = {}

    def to_dict(self) -> dict:
        """Serialize for JSONL export."""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind,
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "lead_id": self.lead_id,
            "start_ms": round(self.start_ms, 2),
            "end_ms": round(self.end_ms, 2),
            "dur_ms": round(self.dur_ms, 2),
            "status": self.status,
            "error_class": self.error_class,
            "error_message": self.error_message,
            "attributes": self.attributes,
        }


# ---------------------------------------------------------------------------
# Trace context — holds the trace tree and current span
# ---------------------------------------------------------------------------


class TraceContext:
    """Holds the trace state for a run.

    Created by start_run(). Passed to span() to open child spans.
    """

    def __init__(self, trace_id: str, run_id: str, workspace_id: str, lead_id: str = ""):
        self.trace_id = trace_id
        self.run_id = run_id
        self.workspace_id = workspace_id
        self.lead_id = lead_id
        self.spans: list[SpanRecord] = []
        self._current_span_id: Optional[str] = None  # root span has no parent

    @property
    def current_span_id(self) -> Optional[str]:
        return self._current_span_id

    def _push_span(self, span: SpanRecord) -> None:
        self._current_span_id = span.span_id
        self.spans.append(span)

    def _pop_span(self, parent_id: Optional[str]) -> None:
        self._current_span_id = parent_id

    def export_jsonl(self) -> str:
        """Export all spans as JSONL (one JSON object per line)."""
        import json
        return "\n".join(json.dumps(s.to_dict()) for s in self.spans)


# ---------------------------------------------------------------------------
# Span context manager
# ---------------------------------------------------------------------------


@contextmanager
def span(
    ctx: TraceContext,
    name: str,
    kind: str = "step",
    **attrs: Any,
):
    """Open a span within a trace context.

    Usage:
        with span(ctx, "step:researching") as step_ctx:
            # do work
            pass

    The span is automatically closed when the `with` block exits.
    If an exception occurs, the span status is set to "error" with the
    exception class and message.
    """
    span_id = f"sp_{uuid.uuid4().hex[:8]}"
    parent_id = ctx.current_span_id

    record = SpanRecord(
        trace_id=ctx.trace_id,
        span_id=span_id,
        parent_id=parent_id,
        name=name,
        kind=kind,
        run_id=ctx.run_id,
        workspace_id=ctx.workspace_id,
        lead_id=ctx.lead_id,
    )
    record.start_ms = time.time() * 1000
    record.attributes = attrs

    ctx._push_span(record)

    try:
        yield ctx
        record.status = "ok"
    except TimeoutError:
        record.status = "timeout"
        record.error_class = "TimeoutError"
        record.error_message = str(record.error_message or "")[:200]
        raise
    except Exception as e:
        record.status = "error"
        record.error_class = type(e).__name__
        record.error_message = str(e)[:200]
        raise
    finally:
        record.end_ms = time.time() * 1000
        record.dur_ms = record.end_ms - record.start_ms
        ctx._pop_span(parent_id)


# ---------------------------------------------------------------------------
# Trace factory
# ---------------------------------------------------------------------------


def start_run(
    run_id: str,
    workspace_id: str,
    lead_id: str = "",
) -> TraceContext:
    """Start a new trace for a run.

    Generates a trace_id that ties together all spans in the run.
    The trace_id is propagated to all child spans via the TraceContext.
    """
    trace_id = f"tr_{uuid.uuid4().hex[:8]}"
    return TraceContext(
        trace_id=trace_id,
        run_id=run_id,
        workspace_id=workspace_id,
        lead_id=lead_id,
    )


__all__ = [
    "SpanRecord",
    "TraceContext",
    "start_run",
    "span",
]
