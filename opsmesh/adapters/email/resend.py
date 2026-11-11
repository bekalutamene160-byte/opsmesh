"""
OPSMESH Resend Email Provider — real email sending + status retrieval.

Architecture:
  Implements the EmailProvider Protocol using httpx.
  Resend's API:
    POST /emails — send email, returns { id, ... }
    GET  /emails/{id} — retrieve email status

  MockEmailProvider remains the default for CI. This adapter is used when
  OPSMESH_EMAIL_PROVIDER=resend + OPSMESH_EMAIL_API_KEY is set.

Reconciliation:
  Resend supports GET /emails/{id} which returns the email's current status.
  This enables the EmailSendReconciler to resolve SEND_UNKNOWN runs:
    - delivered/sent → confirmed_sent → SENT
    - bounced/failed → confirmed_failed → FAILED
    - queued/pending → unknown → retry (max 5)

  Verified: Resend API returns one of: queued, sent, delivered, bounced,
  failed. We map these to our EmailStatus enum.
"""

from __future__ import annotations

from typing import Optional

import httpx

from opsmesh.adapters.email.base import (
    EmailMessage,
    EmailProvider,
    EmailSendResult,
    EmailStatus,
)
from opsmesh.models import utcnow


RESEND_BASE_URL = "https://api.resend.com"

# Map Resend status to our EmailStatus
STATUS_MAP = {
    "queued": "unknown",       # still processing → unknown (retry)
    "sent": "confirmed_sent",  # provider confirmed acceptance
    "delivered": "confirmed_sent",  # confirmed delivered
    "bounced": "confirmed_failed",  # delivery failed
    "failed": "confirmed_failed",   # send failed
    "cancelled": "confirmed_failed",
}


class ResendEmailProvider:
    """
    Resend email provider. Implements EmailProvider Protocol.

    Uses httpx to call Resend's API.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=RESEND_BASE_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(10.0, connect=10.0),
            )
        return self._client

    async def send(self, msg: EmailMessage) -> EmailSendResult:
        """Send an email via Resend. Returns the provider's message_id."""
        client = await self._get_client()

        payload = {
            "from": msg.from_,
            "to": msg.to,
            "subject": msg.subject,
            "text": msg.body,
        }

        # Add idempotency header if provided
        headers = {}
        if msg.idempotency_key:
            headers["Idempotency-Key"] = msg.idempotency_key

        response = await client.post("/emails", json=payload, headers=headers)

        if response.status_code == 429:
            from opsmesh.retry import RateLimitError
            retry_after = response.headers.get("retry-after")
            raise RateLimitError(
                "Resend rate limit exceeded",
                retry_after=float(retry_after) if retry_after else None,
            )

        if response.status_code >= 500:
            from opsmesh.retry import TransientError
            raise TransientError(
                f"Resend server error: {response.status_code}"
            )

        if response.status_code >= 400:
            raise ValueError(
                f"Resend API error: {response.status_code} {response.text[:200]}"
            )

        data = response.json()
        message_id = data.get("id", "")

        if not message_id:
            raise ValueError("Resend returned no message_id")

        # Resend returns 200 with the message_id — the email is accepted
        # but may not be delivered yet (status='queued' or 'sent')
        return EmailSendResult(
            message_id=message_id,
            sent_at=utcnow(),
            status="sent",  # accepted by provider
        )

    async def get_status(self, message_id: str) -> EmailStatus:
        """Query Resend for the status of a previously-sent email.

        Used by the EmailSendReconciler to resolve SEND_UNKNOWN runs.
        """
        client = await self._get_client()

        try:
            response = await client.get(f"/emails/{message_id}")
        except Exception:
            # Network error → unknown (don't fail)
            return EmailStatus(
                message_id=message_id,
                status="unknown",
                detail="Network error querying Resend",
            )

        if response.status_code == 404:
            return EmailStatus(
                message_id=message_id,
                status="unknown",
                detail="Email not found in Resend",
            )

        if response.status_code >= 500:
            return EmailStatus(
                message_id=message_id,
                status="unknown",
                detail=f"Resend server error: {response.status_code}",
            )

        if response.status_code >= 400:
            return EmailStatus(
                message_id=message_id,
                status="unknown",
                detail=f"Resend API error: {response.status_code}",
            )

        data = response.json()
        resend_status = data.get("status", "unknown")

        mapped = STATUS_MAP.get(resend_status, "unknown")

        return EmailStatus(
            message_id=message_id,
            status=mapped,
            detail=f"Resend status: {resend_status}",
        )

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


__all__ = ["ResendEmailProvider"]
