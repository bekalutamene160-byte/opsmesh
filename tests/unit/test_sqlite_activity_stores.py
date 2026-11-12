"""Stage B Phase 4 regression tests: durable SQLite activity stores.

Verifies that the sent_email, lead_history, and contact_policy stores persist
across store instances (simulating a process restart) when a SQLite DB is
configured.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from opsmesh.adapters.db.connection import configure_backend, reset_db_state, init_db
from opsmesh.models import LeadHistoryEvent, SentEmail


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    """Configure a fresh SQLite backend for each test."""
    reset_db_state()
    db_path = str(tmp_path / "test_activity.db")
    configure_backend(database_path=db_path, database_url=None)
    # init_db is async — run it via asyncio.run (fresh loop per call).
    import asyncio
    asyncio.run(init_db(database_path=db_path, database_url=None))
    yield db_path
    reset_db_state()


class TestSQLiteSentEmailStore:
    @pytest.mark.asyncio
    async def test_sent_email_persists_across_instances(self, sqlite_db):
        """A sent-email saved by one store instance is visible to a fresh
        instance (simulating a process restart)."""
        from opsmesh.adapters.db.sqlite_activity_stores import SQLiteSentEmailStore

        # Instance 1 (the worker's specialist writes here).
        store1 = SQLiteSentEmailStore()
        email = SentEmail(
            workspace_id="ws_1", lead_id="lead_1", run_id="run_1",
            recipient="founder@acme.com", from_address="OpsMesh <onboarding@resend.dev>",
            subject="Hello", body="Body content", status="sent",
            provider_message_id="msg_001",
        )
        await store1.save(email)

        # Instance 2 (the API's Activity endpoint reads here after restart).
        store2 = SQLiteSentEmailStore()
        emails, total = await store2.list_recent("ws_1", limit=10)
        assert total == 1
        assert emails[0].recipient == "founder@acme.com"
        assert emails[0].subject == "Hello"

        # get_by_lead + get_by_run
        by_lead = await store2.get_by_lead("ws_1", "lead_1")
        assert len(by_lead) == 1
        by_run = await store2.get_by_run("ws_1", "run_1")
        assert len(by_run) == 1


class TestSQLiteLeadHistoryStore:
    @pytest.mark.asyncio
    async def test_history_persists_across_instances(self, sqlite_db):
        """A history event saved by one instance is visible to a fresh one."""
        from opsmesh.adapters.db.sqlite_activity_stores import SQLiteLeadHistoryStore

        store1 = SQLiteLeadHistoryStore()
        event = LeadHistoryEvent(
            workspace_id="ws_1", lead_id="lead_1", run_id="run_1",
            event_type="send", summary="Email sent: Hello -> founder@acme.com",
            detail="Body content preview",
        )
        await store1.save(event)

        store2 = SQLiteLeadHistoryStore()
        by_lead = await store2.list_by_lead("ws_1", "lead_1")
        assert len(by_lead) == 1
        assert by_lead[0].event_type == "send"
        assert "Email sent" in by_lead[0].summary

        recent, total = await store2.list_recent("ws_1", limit=10)
        assert total == 1


class TestSQLiteContactPolicyStore:
    def test_suppression_persists_across_instances(self, sqlite_db):
        """A suppressed email added by one instance is recognized by a fresh one."""
        from opsmesh.adapters.db.sqlite_activity_stores import SQLiteContactPolicyStore

        store1 = SQLiteContactPolicyStore()
        store1.add("ws_1", "bounced@acme.com", reason="hard_bounce")

        store2 = SQLiteContactPolicyStore()
        assert store2.is_suppressed("ws_1", "bounced@acme.com") is True
        assert store2.is_suppressed("ws_1", "ok@acme.com") is False
        # Case-insensitive.
        assert store2.is_suppressed("ws_1", "BOUNCED@acme.com") is True

        listed = store2.list_for_workspace("ws_1")
        assert len(listed) == 1
        assert listed[0]["reason"] == "hard_bounce"

        # Remove.
        assert store2.remove("ws_1", "bounced@acme.com") is True
        assert store2.is_suppressed("ws_1", "bounced@acme.com") is False
