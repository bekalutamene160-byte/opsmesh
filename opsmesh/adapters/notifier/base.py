"""
Notifier Protocol — internal notifications to the workspace owner.

Used for:
  - Approval requested (Gate 1 / Gate 2 paused).
  - Approval decided (informational).
  - Run failed (DLQ alert).
  - DLQ alert (operator attention required).

Architecture (ADR-008): mock-first. The MockNotifier captures notifications
to a list. Real adapters (EmailNotifier, SlackNotifier) arrive in Day 5.

IMPORTANT (Correction 5):
  The notifier sends a NOTIFICATION containing an authenticated dashboard
  link. The approver clicks the link, logs in, and approves via the
  dashboard. Approval NEVER happens by replying to the notification.
  Email/Slack is one-way (notification only); the dashboard is the
  authoritative approval surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from opsmesh.models import utcnow


class NotificationResult(BaseModel):
    """Result of sending a notification."""

    notification_id: str
    sent_at: datetime = Field(default_factory=utcnow)
    channel: Literal["email", "slack"] = "email"


@runtime_checkable
class Notifier(Protocol):
    """The notifier contract."""

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        kind: str,
    ) -> NotificationResult:
        """
        Send a notification.

        Args:
            to: the recipient (workspace_owner for v1).
            subject: notification subject.
            body: notification body — must include an authenticated
                dashboard link for approval kinds (Correction 5).
            kind: one of 'approval_requested', 'approval_decision',
                'run_failed', 'dlq_alert'.
        """
        ...


__all__ = ["NotificationResult", "Notifier"]
