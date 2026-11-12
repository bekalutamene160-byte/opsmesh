"""OPSMESH rate-limit singleton (Stage B Phase 1, P0-5).

A single ``Limiter`` instance shared by every route module. Route handlers
apply ``@limiter.limit("N/min")`` decorators to public write endpoints
(POST /events/*, POST /leads, POST /demo/simulate) so cost-abuse via
repeated webhook/lead-submission is throttled per-IP.

The limiter uses an in-memory backend (per-process). For multi-worker
production, set ``storage_uri`` to a Redis URL — documented in
``docs/audit/DEPLOYMENT_BASELINE.md``. The single-instance demo deploy is
fine with the in-memory backend.

``create_app()`` attaches this limiter to ``app.state.limiter`` and
registers the ``SlowAPIMiddleware`` + the ``RateLimitExceeded`` exception
handler (returning a structured 429). Route decorators only need the
module-level ``limiter`` singleton below; they do not need the app.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

# Per-IP key. In single_user_demo mode there is no authenticated user, so
# IP is the only available throttle key. (For multi-user prod, switch to
# user_id-keyed limits via a custom key_func.)
limiter: Limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")


# Public write-endpoint limits (Engineering Contract §2).
LEAD_WRITE_LIMIT = "10/min"        # POST /leads
SIMULATE_LIMIT = "5/min"           # POST /demo/simulate (fabricates leads)
WEBHOOK_LIMIT = "30/min"           # POST /events/new_lead (signed — slightly higher)
TALLY_LIMIT = "30/min"             # POST /events/tally (signed)
APPROVAL_DECISION_LIMIT = "30/min" # POST /approvals/{id}/decision


__all__ = [
    "limiter",
    "LEAD_WRITE_LIMIT",
    "SIMULATE_LIMIT",
    "WEBHOOK_LIMIT",
    "TALLY_LIMIT",
    "APPROVAL_DECISION_LIMIT",
]
