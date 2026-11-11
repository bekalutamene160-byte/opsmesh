"""
MockNotifier — captures notifications to a list (for test assertions).

Architecture (ADR-008): mock-first. Never sends real email or Slack messages.
"""

from __future__ import annotations

from uuid import uuid4

from opsmesh.adapters.notifier.base import Notifier, NotificationResult


class MockNotifier:
    """
    Mock notifier. Captures every notification sent.

    Usage:
        notifier = MockNotifier()
        await notifier.send("workspace_owner", "Subject", "Body", "approval_requested")
        assert len(notifier.sent) == 1
        assert notifier.sent[0].kind == "approval_requested"
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        kind: str,
    ) -> NotificationResult:
        self._call_count += 1
        self.sent.append({
            "to": to,
            "subject": subject,
            "body": body,
            "kind": kind,
        })
        return NotificationResult(
            notification_id=f"notif_{uuid4().hex[:12]}",
            channel="email",
        )


__all__ = ["MockNotifier"]
