"""
MockEmailProvider — captures sent emails to a list (for test assertions).

Architecture (ADR-008): mock-first. This provider:
  - Stores every sent email in a list (testable).
  - Supports injected failures (timeout, provider_error) for failure-injection
    tests (Day 3).
  - Never makes a network call.

Day 2 default behavior: every send() succeeds with status='sent'.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from opsmesh.adapters.email.base import (
    EmailMessage,
    EmailProvider,
    EmailSendResult,
    EmailStatus,
)
from opsmesh.models import utcnow


class ProviderError(Exception):
    """Raised when the email provider returns a confirmed failure."""


class MockEmailProvider:
    """
    Mock email provider. Captures sends; supports failure injection.

    Usage:
        email = MockEmailProvider()
        result = await email.send(EmailMessage(...))
        assert len(email.sent_messages) == 1

        # Inject a timeout for the next send (failure-injection test)
        email.inject_timeout()
        with pytest.raises(asyncio.TimeoutError):
            await email.send(...)
    """

    def __init__(self) -> None:
        self.sent_messages: list[EmailMessage] = []
        self._call_count = 0
        # Failure injection controls
        self._next_send_times_out = False
        self._next_send_fails = False
        # Status overrides for reconciliation tests
        self._status_overrides: dict[str, EmailStatus] = {}

    @property
    def call_count(self) -> int:
        return self._call_count

    def inject_timeout(self) -> None:
        """Make the NEXT send() raise asyncio.TimeoutError (ambiguous send)."""
        self._next_send_times_out = True

    def inject_failure(self) -> None:
        """Make the NEXT send() raise ProviderError (confirmed failure)."""
        self._next_send_fails = True

    def set_status_override(
        self, message_id: str, status: EmailStatus
    ) -> None:
        """Override get_status() for a specific message_id (reconciler tests)."""
        self._status_overrides[message_id] = status

    async def send(self, msg: EmailMessage) -> EmailSendResult:
        self._call_count += 1

        if self._next_send_times_out:
            self._next_send_times_out = False
            raise asyncio.TimeoutError(
                f"Injected timeout sending email to {msg.to}"
            )

        if self._next_send_fails:
            self._next_send_fails = False
            raise ProviderError("Injected provider failure")

        # Capture the sent message
        self.sent_messages.append(msg)

        return EmailSendResult(
            message_id=msg.idempotency_key or f"msg_{self._call_count:08d}",
            status="sent",
        )

    async def get_status(self, message_id: str) -> EmailStatus:
        """Return the status of a sent email. Default: confirmed_sent.
        Tests can override via set_status_override()."""
        if message_id in self._status_overrides:
            return self._status_overrides[message_id]
        # Default: every sent email is confirmed sent
        return EmailStatus(
            message_id=message_id,
            status="confirmed_sent",
            detail="Mock provider confirms send.",
        )


__all__ = ["MockEmailProvider", "ProviderError"]
