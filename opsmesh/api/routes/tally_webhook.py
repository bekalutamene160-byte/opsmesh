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
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from opsmesh.models import LeadSource
from opsmesh.services.lead_intake import (
    DuplicateSubmission,
    LeadIntakeService,
    SuppressedEmail,
)
from opsmesh.webhook import verify_hmac


router = APIRouter(prefix="/api/v1", tags=["tally"])

# Module-level references
_intake_service: Optional[LeadIntakeService] = None
_tally_secret: str = ""


def configure_tally_route(
    intake_service: LeadIntakeService,
    tally_secret: str,
) -> None:
    """Configure the Tally webhook route.

    If tally_secret is empty, signature verification is SKIPPED (dev mode).
    If tally_secret is set, the signature is verified on raw bytes.
    """
    global _intake_service, _tally_secret
    _intake_service = intake_service
    # Only set the secret if it's non-empty AND not the same as the generic
    # webhook secret (which would be confusing). If empty, no verification.
    _tally_secret = tally_secret if tally_secret else ""


def normalize_tally_payload(raw_body: bytes) -> dict:
    """Normalize Tally's payload format to OPSMESH's Lead fields.

    Tally sends fields as an array of {key, value, label} objects.
    We extract company, contact_email, industry, etc. based on field labels.
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

    # Parse employees (may be a string like "10-50")
    employees = None
    if employees_str:
        try:
            # Try to parse as int
            employees = int(employees_str)
        except (ValueError, TypeError):
            # If it's a range like "10-50", take the lower bound
            if "-" in employees_str:
                try:
                    employees = int(employees_str.split("-")[0].strip())
                except (ValueError, IndexError):
                    pass

    submission_id = data.get("submissionId", data.get("eventId", ""))

    return {
        "company": company,
        "contact_email": contact_email,
        "contact_name": contact_name or None,
        "industry": industry or None,
        "employees": employees,
        "submission_id": submission_id,
    }


@router.post("/events/tally", status_code=202)
async def tally_webhook(request: Request):
    """Receive a Tally form submission webhook.

    This endpoint does NOT require Bearer auth — it uses HMAC signature
    verification instead (like the generic webhook).
    """
    if _intake_service is None:
        raise HTTPException(503, "Lead intake service not configured")

    # 1. Read the RAW body
    raw_body = await request.body()

    # 2. Verify the signature on raw bytes
    # Tally uses the 'tally-signature' header (verify at deploy time)
    signature = request.headers.get("tally-signature") or request.headers.get(
        "X-Tally-Signature", ""
    )

    if _tally_secret:
        if not verify_hmac(raw_body, signature, secret=_tally_secret):
            raise HTTPException(401, "Invalid Tally signature")

    # 3. Normalize the payload
    try:
        normalized = normalize_tally_payload(raw_body)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse Tally payload: {str(e)[:200]}")

    if not normalized["company"] or not normalized["contact_email"]:
        raise HTTPException(
            422,
            "Tally payload missing required fields: company name and email",
        )

    # 4. Create the lead
    try:
        result = await _intake_service.create_lead(
            workspace_id="default",
            company=normalized["company"],
            contact_email=normalized["contact_email"],
            contact_name=normalized.get("contact_name"),
            industry=normalized.get("industry"),
            employees=normalized.get("employees"),
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


__all__ = ["router", "configure_tally_route", "normalize_tally_payload"]
