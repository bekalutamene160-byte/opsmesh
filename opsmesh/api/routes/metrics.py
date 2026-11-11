"""
OPSMESH Metrics Route — GET /api/v1/metrics.

Returns the 8 minimum-viable metrics as JSON.

Architecture (§12 Observability):
  The /metrics endpoint returns JSON (not Prometheus format; v1 does not
  require Prometheus). The dashboard and alerting systems read from here.

  Response shape: see opsmesh.observability.metrics.compute_metrics()
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from opsmesh.observability.metrics import compute_metrics


router = APIRouter(prefix="/api/v1", tags=["metrics"])

# Module-level references — set by configure_metrics_route()
_store = None
_queue = None
_workspace_id = "default"


def configure_metrics_route(store, queue=None, workspace_id: str = "default") -> None:
    """Configure the metrics route with the given store + queue."""
    global _store, _queue, _workspace_id
    _store = store
    _queue = queue
    _workspace_id = workspace_id


@router.get("/metrics")
async def get_metrics():
    """Return the 8 minimum-viable metrics as JSON.

    Computes metrics on-demand from the StateStore (no separate metrics
    store in v1). For larger scale, Day 7 will add a materialized view.
    """
    if _store is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Metrics not configured — store is not set"},
        )

    metrics = await compute_metrics(
        store=_store,
        workspace_id=_workspace_id,
        queue=_queue,
    )
    return metrics


__all__ = ["router", "configure_metrics_route"]
