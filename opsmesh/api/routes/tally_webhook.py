"""
OPSMESH Tally Webhook — Tally-specific inbound lead events.

Architecture:
  Tally is an external form provider. When someone submits a form on Tally,
  Tally sends a webhook to OPSMESH. This endpoint:

    1. Verifies the Tally signature (raw body, not re-serialized JSON).
    2. Normalizes Tally's payload format to OPSMESH's Lead model.
    3. Deduplicates via Tally's submission ID.
    4. Enqueues a job via LeadIntakeService.
    5. Returns 202 Accepted.

  The generic HMAC webhook (POST /api/v1/events/new_lead) is PRESERVED.
  Both endpoints coexist. Tally has its own signature format and payload
  structure, which differs from the generic webhook.

TALLY PAYLOAD FORMAT (example):
  {
    "eventId": "tally_event_abc123",
    "formId": "form_xyz",
    "submissionId": "sub_001",
    "fields": [
      {"key": "question_q1", "value": "Acme Corp", "label": "Company Name"},
      {"key": "question_q2", "value": "founder@acme.com", "label": "Email"},
      {"key": "question_q3", "value": "Software", "label": "Industry"},
    ],
    "createdAt": "2026-11-09T12:00:00Z"
  }

SIGNATURE:
  Tally sends a signature in the `tally-signature` header.
  The signature is HMAC-SHA256 of the raw body using a shared secret.
  (Verify the exact header name and algorithm from Tally's docs at
  deploy time.)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, TypeAdapter, ValidationError

from opsmesh.api.rate_limit import TALLY_LIMIT, limiter
from opsmesh.config import get_settings, is_demo_mode, resolve_bound_workspace
from opsmesh.models import LeadSource
from opsmesh.services.lead_intake import (
    DuplicateSubmission,
    LeadIntakeService,
    SuppressedEmail,
)
from opsmesh.webhook import verify_hmac, WebhookSignatureError


logger = logging.getLogger(__name__)

# Pydantic v2: EmailStr is a type, not a callable. Use a TypeAdapter to
# validate a plain string against the EmailStr constraints.
_EMAIL_ADAPTER = TypeAdapter(EmailStr)

router = APIRouter(prefix="/api/v1", tags=["tally"])

# Module-level references
_intake_service: Optional[LeadIntakeService] = None
_tally_secret: str = ""
_workspace_id: str = "default"


def configure_tally_route(
    intake_service: LeadIntakeService,
    tally_secret: str,
    workspace_id: Optional[str] = None,
) -> None:
    """Configure the Tally webhook route.

    Stage B Phase 1 (P0-2): the HMAC check is UNCONDITIONAL when a secret is
    configured. In production_mode, a missing secret fails-closed at startup
    (app.py) AND at request time (401). In dev/demo mode with no secret,
    verification is skipped for local testing only — prod can never reach
    that state because the startup check rejects it.

    Stage B Phase 1 (P0-6): the workspace is bound server-side to
    `settings.demo_workspace_id` (in single_user_demo mode) and NEVER taken
    from the request body.
    """
    global _intake_service, _tally_secret, _workspace_id
    _intake_service = intake_service
    _tally_secret = tally_secret or ""
    # Resolve the server-bound workspace. In demo mode this is the configured
    # demo workspace; the browser cannot override it.
    if workspace_id is not None:
        _workspace_id = workspace_id
    else:
        try:
            _workspace_id = resolve_bound_workspace()
        except RuntimeError:
            # multi_user mode: workspace resolution is the auth layer's job;
            # the Tally webhook is unauthenticated, so bind to "default" only
            # if explicitly in multi_user mode (rare for webhook intake).
            _workspace_id = "default"


def normalize_tally_payload(raw_body: bytes) -> dict:
    """Normalize Tally's payload format to OPSMESH's Lead fields.

    Tally sends fields as an array of {key, value, label} objects.
    We extract company, contact_email, industry, etc. based on field labels.

    Supports individual leads (no company) -- if the form has a "lead type"
    field set to "individual", or if no company is provided but a name + email
    are, the lead is treated as an individual.
    """
    data = json.loads(raw_body)

    # Extract fields from Tally's format
    fields = {}
    for field in data.get("fields", []):
        label = field.get("label", "").lower()
        value = field.get("value", "")
        fields[label] = value

    # Map Tally fields to OPSMESH fields (by label keyword matching)
    company = (
        fields.get("company name", "")
        or fields.get("company", "")
        or fields.get("organization", "")
    )
    contact_email = (
        fields.get("email", "")
        or fields.get("email address", "")
        or fields.get("work email", "")
    )
    contact_name = (
        fields.get("name", "")
        or fields.get("full name", "")
        or fields.get("contact name", "")
    )
    industry = (
        fields.get("industry", "")
        or fields.get("sector", "")
    )
    employees_str = (
        fields.get("employees", "")
        or fields.get("company size", "")
    )
    website = (
        fields.get("website", "")
        or fields.get("url", "")
        or fields.get("domain", "")
    )
    # Free-form notes/context the person filled (what they want, their situation)
    notes = (
        fields.get("notes", "")
        or fields.get("message", "")
        or fields.get("what can we help with", "")
        or fields.get("how can we help", "")
        or fields.get("details", "")
    )
    # Lead type: explicit field, or auto-detect from company presence
    lead_type_raw = (
        fields.get("lead type", "")
        or fields.get("are you submitting as", "")
        or fields.get("submitting as", "")
    ).lower()
    if "individual" in lead_type_raw or "person" in lead_type_raw:
        lead_type = "individual"
    elif "company" in lead_type_raw or "business" in lead_type_raw:
        lead_type = "company"
    else:
        # Auto-detect: if no company but we have a name, it's an individual
        lead_type = "individual" if (not company and contact_name) else "company"

    # Parse employees (may be a string like "10-50")
    employees = None
    if employees_str:
        try:
            employees = int(employees_str)
        except (ValueError, TypeError):
            if "-" in employees_str:
                try:
                    employees = int(employees_str.split("-")[0].strip())
                except (ValueError, IndexError):
                    pass

    submission_id = data.get("submissionId", data.get("eventId", ""))

    # Stage B Phase 1 (P0-2): validate contact_email format. An unsigned/loose
    # Tally payload must not inject an arbitrary string into the email pipeline.
    # EmailStr validation runs in the route handler (below) via _validate_email;
    # here we just normalize to None when empty.
    return {
        "lead_type": lead_type,
        "company": company,
        "contact_email": contact_email,
        "contact_name": contact_name or None,
        "industry": industry or None,
        "employees": employees,
        "website": website or None,
        "notes": notes or None,
        "submission_id": submission_id,
    }


def _validate_email(email: str) -> str:
    """Validate a contact email using Pydantic EmailStr.

    Raises HTTPException(422) if the email is malformed. This is the P0-2
    defense: the Tally `contact_email` field had no validation, so an
    attacker could inject arbitrary strings into the email pipeline.
    """
    if not email:
        raise HTTPException(422, "Tally payload missing required field: email")
    try:
        validated = _EMAIL_ADAPTER.validate_python(email)
    except (ValidationError, ValueError) as e:
        raise HTTPException(422, f"Invalid contact_email: {str(e)[:120]}")
    return str(validated)


async def _process_tally_submission(raw_body: bytes) -> dict:
    """Shared post-authentication processing for both Tally routes.

    Normalizes the raw Tally payload, validates the email, enforces the
    company-required rule for company leads, and creates the lead via the
    intake service. Returns the JSON response body (status/duplicate/etc).
    """
    if _intake_service is None:
        raise HTTPException(503, "Lead intake service not configured")

    # Normalize the payload
    try:
        normalized = normalize_tally_payload(raw_body)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse Tally payload: {str(e)[:200]}")

    # Validate the contact email (P0-2: no arbitrary strings into the pipeline)
    normalized["contact_email"] = _validate_email(normalized["contact_email"])

    # For company leads, require company + email. For individual leads,
    # only require email (the person may not have a company).
    if normalized["lead_type"] == "company" and not normalized["company"]:
        raise HTTPException(
            422,
            "Tally payload missing required field: company name (for company leads)",
        )

    # Create the lead — workspace is server-bound, never from the request.
    try:
        result = await _intake_service.create_lead(
            workspace_id=_workspace_id,
            lead_type=normalized["lead_type"],
            company=normalized["company"],
            contact_email=normalized["contact_email"],
            contact_name=normalized.get("contact_name"),
            industry=normalized.get("industry"),
            employees=normalized.get("employees"),
            website=normalized.get("website"),
            notes=normalized.get("notes"),
            source=LeadSource.WEBHOOK,
            submission_id=normalized["submission_id"],
        )
    except DuplicateSubmission as e:
        return {"status": "duplicate", "submission_id": e.submission_id}
    except SuppressedEmail:
        return {"status": "accepted"}  # deliberately vague

    return {
        "status": "accepted",
        "lead_id": result.lead_id,
        "run_id": result.run_id,
    }


@router.post("/events/tally", status_code=202)
@limiter.limit(TALLY_LIMIT)
async def tally_webhook(request: Request):
    """Receive a Tally form submission webhook (HMAC-signed variant).

    Stage B Phase 1 (P0-2): HMAC signature verification is UNCONDITIONAL when
    a secret is configured. In production_mode, a missing secret fails-closed
    at startup (app.py) AND here (401). In dev/demo mode with no secret,
    verification is skipped for local testing only.

    NOTE: Tally does NOT sign webhooks by default. For Tally (and other
    unsigned form providers), use the URL-token variant:
    POST /api/v1/events/tally/{token} — the token in the path authenticates.
    This HMAC variant is kept for providers that DO sign payloads.

    Stage B Phase 1 (P0-6): the workspace is bound server-side to
    `settings.demo_workspace_id`; the request body never supplies it.
    """
    # 1. Read the RAW body
    raw_body = await request.body()

    # 2. Verify the signature on raw bytes (UNCONDITIONAL when secret is set).
    signature = request.headers.get("tally-signature") or request.headers.get(
        "X-Tally-Signature", ""
    )

    settings = get_settings()
    if _tally_secret:
        # Secret configured -> ALWAYS verify. No skip path.
        try:
            if not verify_hmac(raw_body, signature, secret=_tally_secret):
                raise HTTPException(401, "Invalid Tally signature")
        except WebhookSignatureError:
            raise HTTPException(401, "Tally signature verification failed")
    elif settings.production_mode:
        raise HTTPException(401, "Tally webhook secret not configured")
    # else: dev/demo mode with no secret -> skip verification (local testing).

    return await _process_tally_submission(raw_body)


@router.post("/events/tally/{token}", status_code=202)
@limiter.limit(TALLY_LIMIT)
async def tally_webhook_token(request: Request, token: str):
    """Receive a Tally submission via URL-embedded token (unsigned variant).

    Tally (and most no-code form tools: Webflow, Typeform, Zapier) do NOT
    sign webhook payloads. Instead, the secret is embedded in the webhook
    URL itself: the customer configures Tally to POST to
    ``https://your-host/api/v1/events/tally/<secret>``. The token in the
    path authenticates the request.

    This is the same pattern Slack, Stripe, and Tally itself use for
    unsigned integrations. It is secure as long as the URL (and thus the
    token) is known only to OPSMESH and the customer's form provider.

    Auth:
      - If a Tally secret is configured: token must match it (constant-time).
      - In production with NO secret: fail closed (401).
      - In dev/demo with no secret: accept any token (local testing).
    """
    import hmac as _hmac

    settings = get_settings()
    if _tally_secret:
        if not _hmac.compare_digest(token, _tally_secret):
            raise HTTPException(401, "Invalid Tally webhook token")
    elif settings.production_mode:
        raise HTTPException(401, "Tally webhook secret not configured")
    # else: dev/demo mode with no secret -> accept (local testing).

    raw_body = await request.body()
    return await _process_tally_submission(raw_body)


__all__ = ["router", "configure_tally_route", "normalize_tally_payload"]
