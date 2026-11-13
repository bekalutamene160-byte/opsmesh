"""
OPSMESH Webhook Route — authenticated inbound lead events.

Architecture (Engineering Contract §1):
  The webhook handler MUST return 202 within 5 seconds — never run the
  supervisor inline. The handler:
    1. Reads the RAW request body (NOT re-serialized JSON).
    2. Verifies the HMAC signature on the raw bytes (Correction #14).
    3. Checks dedup on submission_id (FR-4).
    4. Enqueues a job via LeadIntakeService.create_lead.
    5. Returns 202 Accepted with {lead_id, run_id, status: "queued"}.

If the signature is invalid → 401.
If the submission is a duplicate → 202 with {status: "duplicate"}.
If the email is suppressed → 202 with {status: "suppressed"} (we return 202,
  not 403, to avoid leaking suppression list state to attackers).

Endpoint: POST /api/v1/events/new_lead
Headers:
  X-Signature: <hex sha256 HMAC of raw body>
  X-Submission-Id: <unique ID for dedup>  (optional; can also be in body)
  Content-Type: application/json
Body: raw JSON bytes (verified as-is, never re-serialized).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field

from opsmesh.api.rate_limit import WEBHOOK_LIMIT, limiter
from opsmesh.config import resolve_bound_workspace
from opsmesh.models import LeadSource
from opsmesh.services.lead_intake import (
    DuplicateSubmission,
    LeadIntakeService,
    SuppressedEmail,
)
from opsmesh.webhook import verify_hmac_or_raise, WebhookSignatureError


router = APIRouter(prefix="/api/v1", tags=["webhook"])

# Module-level variables (updated by configure_webhook_route)
_intake_service = None
_secret = ""
_workspace_id = "default"


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class WebhookPayload(BaseModel):
    """The parsed JSON payload of a new_lead webhook event.

    NOTE: HMAC verification happens on the RAW bytes BEFORE this model is
    constructed. We never trust parsed JSON until the signature is verified.
    """

    company: str = Field(min_length=1, max_length=200)
    contact_email: EmailStr
    contact_name: Optional[str] = None
    industry: Optional[str] = None
    employees: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    website: Optional[str] = None
    submission_id: str = Field(min_length=1)
    received_at: Optional[str] = None  # ISO 8601; we use our own timestamp if absent


class WebhookAccepted(BaseModel):
    """202 Accepted response."""
    status: str = "accepted"
    lead_id: str
    run_id: str


class WebhookDuplicate(BaseModel):
    """202 response for a duplicate submission (FR-4)."""
    status: str = "duplicate"
    submission_id: str


class WebhookSuppressed(BaseModel):
    """202 response for a suppressed email.

    We return 202 (not 403) to avoid leaking suppression list state.
    An attacker probing emails gets the same response as a duplicate."""
    status: str = "accepted"  # deliberately vague
    lead_id: Optional[str] = None  # omitted
    run_id: Optional[str] = None  # omitted


class PublicLeadPayload(BaseModel):
    """Validated payload for the public lead-submission endpoint.

    This is the server-side validation layer (P0-2): the embed widget
    validates client-side too, but a direct API caller can bypass the JS,
    so we validate here. Email format, lead_type enum, and field length
    caps are enforced. A honeypot field (website_hp) is accepted but, if
    non-empty, causes the lead to be silently dropped (bot trap).
    """

    contact_email: EmailStr
    contact_name: Optional[str] = Field(default=None, max_length=200)
    company: Optional[str] = Field(default="", max_length=200)
    industry: Optional[str] = Field(default=None, max_length=120)
    website: Optional[str] = Field(default=None, max_length=300)
    notes: Optional[str] = Field(default=None, max_length=2000)
    employees: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    lead_type: str = Field(default="company", pattern="^(company|individual)$")
    submission_id: Optional[str] = Field(default=None, min_length=1, max_length=200)
    # Honeypot: bots fill this; humans never see it (hidden off-screen in
    # the widget). If present and non-empty, the request is silently
    # accepted (202) but NO lead is created. The field name is innocuous
    # so bots that auto-fill every field trip it.
    website_hp: Optional[str] = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


def configure_webhook_route(
    intake_service: LeadIntakeService,
    webhook_secret: str,
    workspace_id: Optional[str] = None,
) -> None:
    """Configure the webhook route with the given service and secret.

    Uses module-level variables so that each create_app() call updates
    the service and secret used by the handler.

    Stage B Phase 1 (P0-6): the workspace is bound server-side to
    `settings.demo_workspace_id` (in single_user_demo mode). The request
    body NEVER supplies the workspace_id.
    """
    global _intake_service, _secret, _workspace_id
    _intake_service = intake_service
    _secret = webhook_secret
    if workspace_id is not None:
        _workspace_id = workspace_id
    else:
        try:
            _workspace_id = resolve_bound_workspace()
        except RuntimeError:
            _workspace_id = "default"

    @router.post("/events/new_lead", status_code=202)
    @limiter.limit(WEBHOOK_LIMIT)
    async def new_lead_webhook(request: Request):
        """Authenticated webhook for inbound lead events.

        Returns 202 Accepted on success (lead created + job enqueued).
        Returns 401 on invalid signature.
        Returns 202 with status=duplicate on duplicate submission_id.

        Stage B Phase 1 (P0-5): rate-limited per-IP (signed endpoint, 30/min).
        """
        # 1. Read the RAW body — NOT re-serialized JSON.
        #    The HMAC is computed on the exact bytes received.
        raw_body = await request.body()

        # 2. Verify HMAC on raw bytes (Correction #14).
        signature = request.headers.get("X-Signature")
        try:
            verify_hmac_or_raise(raw_body, signature, secret=_secret)
        except WebhookSignatureError:
            raise HTTPException(status_code=401, detail="Invalid signature")

        # 3. NOW we can parse the JSON (signature verified).
        import json
        try:
            payload_data = json.loads(raw_body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        # 4. Validate against the schema.
        try:
            payload = WebhookPayload(**payload_data)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Validation error: {e}")

        # 5. Create the lead via LeadIntakeService.
        #    Workspace is server-bound (P0-6) — never derived from the secret
        #    or the request body in single_user_demo mode.
        try:
            result = await _intake_service.create_lead(
                workspace_id=_workspace_id,
                company=payload.company,
                contact_email=str(payload.contact_email),
                contact_name=payload.contact_name,
                industry=payload.industry,
                employees=payload.employees,
                website=payload.website,
                source=LeadSource.WEBHOOK,
                submission_id=payload.submission_id,
            )
        except DuplicateSubmission as e:
            return WebhookDuplicate(submission_id=e.submission_id)
        except SuppressedEmail:
            # Deliberately vague response — don't leak suppression state.
            return WebhookSuppressed()

        return WebhookAccepted(
            lead_id=result.lead_id,
            run_id=result.run_id,
        )

    # ------------------------------------------------------------------
    # PUBLIC lead endpoint — for the embed widget (no backend, no HMAC).
    # Auth: a publishable token (OPSMESH_PUBLIC_TOKEN) sent as
    # `Authorization: Bearer pk_live_...`. The token can ONLY create leads
    # — never read, send, or approve. CORS is open for this path so static
    # sites can POST directly from the browser.
    #
    # Stage B Hotfix v4 (integration hardening):
    #   - Pydantic-validated payload (email format, lead_type enum, caps).
    #   - Honeypot field (website_hp) silently drops bot submissions.
    #   - GET /public/status reports whether the endpoint is enabled
    #     WITHOUT leaking the token value.
    # ------------------------------------------------------------------
    PUBLIC_LEAD_LIMIT = "10/minute"

    def _public_token() -> str:
        from opsmesh.config import get_settings
        return getattr(get_settings(), "public_token", "") or ""

    @router.get("/public/status", status_code=200)
    async def public_lead_status():
        """Report whether the public lead endpoint is enabled.

        Returns {enabled: bool}. Does NOT reveal the token value. Used by
        the Integrations page to show the embed-widget status and by
        deploy-time smoke tests.
        """
        return {"enabled": bool(_public_token())}

    @router.post("/public/lead", status_code=202)
    @limiter.limit(PUBLIC_LEAD_LIMIT)
    async def public_lead(request: Request):
        """Public lead-submission endpoint for the embed widget.

        Headers: Authorization: Bearer <OPSMESH_PUBLIC_TOKEN>
        Body: PublicLeadPayload (validated: email, lead_type, caps)

        Returns 202 + {lead_id, run_id} on success.
        Returns 401 if the public token is not configured or doesn't match.
        Returns 422 on validation error (bad email, bad lead_type, etc.).
        Returns 202 with status=duplicate on duplicate submission_id.

        Honeypot: if `website_hp` is non-empty, returns 202 accepted but
        creates NO lead (bot trap - the bot can't tell it was dropped).

        NOTE: the body is read raw and parsed manually (matching
        new_lead_webhook in this file) rather than declared as a Pydantic
        body parameter. With `from __future__ import annotations` active,
        FastAPI does not resolve a model annotation on a nested function as
        a body param (it falls back to query), so we validate explicitly
        here via PublicLeadPayload.model_validate().
        """
        public_token = _public_token()
        if not public_token:
            raise HTTPException(
                status_code=401,
                detail="Public lead endpoint is not enabled. Set OPSMESH_PUBLIC_TOKEN on the server.",
            )
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token.")
        provided = auth[7:].strip()
        # Constant-time comparison to resist timing probes.
        import hmac as _hmac
        if not _hmac.compare_digest(provided, public_token):
            raise HTTPException(status_code=401, detail="Invalid publishable token.")

        raw_body = await request.body()
        import json as _json
        try:
            data = _json.loads(raw_body) if raw_body else {}
        except _json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON body.")
        try:
            payload = PublicLeadPayload.model_validate(data)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Validation error: {str(e)[:300]}")

        # Honeypot: silently swallow bot submissions. Return the same 202
        # shape a real success would have (minus the ids) so the bot can't
        # distinguish a dropped submission from a real one.
        if payload.website_hp:
            return {"status": "accepted"}

        # For company leads, require a company name (the widget enforces
        # this client-side too, but a direct API caller must be held to it).
        if payload.lead_type == "company" and not (payload.company or "").strip():
            raise HTTPException(
                status_code=422,
                detail="company is required for lead_type='company'.",
            )

        submission_id = payload.submission_id or f"pub_{id(payload)}"

        try:
            result = await _intake_service.create_lead(
                workspace_id=_workspace_id,
                company=payload.company or "",
                contact_email=str(payload.contact_email),
                contact_name=payload.contact_name,
                industry=payload.industry,
                employees=payload.employees,
                website=payload.website,
                notes=payload.notes,
                lead_type=payload.lead_type,
                source=LeadSource.WEBHOOK,
                submission_id=submission_id,
            )
        except DuplicateSubmission as e:
            return {"status": "duplicate", "submission_id": e.submission_id}
        except SuppressedEmail:
            return {"status": "suppressed"}

        return {"status": "accepted", "lead_id": result.lead_id, "run_id": result.run_id}


__all__ = ["router", "configure_webhook_route", "WebhookPayload", "WebhookAccepted", "PublicLeadPayload"]
