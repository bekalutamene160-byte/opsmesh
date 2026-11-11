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


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


def configure_webhook_route(
    intake_service: LeadIntakeService,
    webhook_secret: str,
) -> None:
    """Configure the webhook route with the given service and secret.

    Uses module-level variables so that each create_app() call updates
    the service and secret used by the handler.
    """
    global _intake_service, _secret
    _intake_service = intake_service
    _secret = webhook_secret

    @router.post("/events/new_lead", status_code=202)
    async def new_lead_webhook(request: Request):
        """Authenticated webhook for inbound lead events.

        Returns 202 Accepted on success (lead created + job enqueued).
        Returns 401 on invalid signature.
        Returns 202 with status=duplicate on duplicate submission_id.
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
        try:
            result = await _intake_service.create_lead(
                workspace_id="default",  # Day 6: derive from webhook secret
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


__all__ = ["router", "configure_webhook_route", "WebhookPayload", "WebhookAccepted"]
