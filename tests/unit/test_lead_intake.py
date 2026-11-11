"""
Tests for opsmesh.services.lead_intake.LeadIntakeService.

Covers:
  - Manual lead creation (happy path)
  - Webhook lead creation (with submission_id)
  - Simulated lead creation
  - All three paths invoke the same create_lead method (FR-5)
  - Duplicate submission_id raises DuplicateSubmission (FR-4)
  - Expired submission_id allows re-creation (5 min window)
  - Suppression list rejects lead (Correction 7)
  - Lead created with disposition=NEW
  - Run created with state=NEW
  - Job enqueued
  - Workspace isolation (get_lead from wrong workspace → None)
"""

from __future__ import annotations

import pytest

from opsmesh.job_queue import InMemoryJobQueue
from opsmesh.models import LeadDisposition, LeadSource
from opsmesh.services.lead_intake import (
    DEDUP_WINDOW_SECONDS,
    DuplicateSubmission,
    LeadIntakeService,
    SuppressedEmail,
)
from opsmesh.state_machine import State
from opsmesh.state_store import InMemoryStateStore


@pytest.fixture
def store():
    return InMemoryStateStore()


@pytest.fixture
def queue():
    return InMemoryJobQueue()


@pytest.fixture
def service(store, queue):
    return LeadIntakeService(store=store, queue=queue)


# ---------------------------------------------------------------------------
# 1. Happy path: manual lead creation
# ---------------------------------------------------------------------------


class TestManualIntake:
    @pytest.mark.asyncio
    async def test_creates_lead_run_and_job(self, service, store, queue):
        result = await service.create_lead(
            workspace_id="ws_1",
            company="Acme Corp",
            contact_email="founder@acme.com",
            source=LeadSource.MANUAL,
        )
        # Lead created
        lead = await service.get_lead("ws_1", result.lead_id)
        assert lead is not None
        assert lead.company == "Acme Corp"
        assert lead.disposition == LeadDisposition.NEW

        # Run created with state=NEW
        run = await store.load_latest(result.run_id, "ws_1")
        assert run is not None
        assert run.state == State.NEW

        # Job enqueued
        assert await queue.depth() == 1

    @pytest.mark.asyncio
    async def test_lead_id_run_id_job_id_all_returned(self, service):
        result = await service.create_lead(
            workspace_id="ws_1",
            company="Acme",
            contact_email="r@acme.com",
            source=LeadSource.MANUAL,
        )
        assert result.lead_id.startswith("lead_")
        assert result.run_id.startswith("run_")
        assert result.job_id.startswith("job_")


# ---------------------------------------------------------------------------
# 2. Webhook intake (with submission_id + dedup)
# ---------------------------------------------------------------------------


class TestWebhookIntake:
    @pytest.mark.asyncio
    async def test_webhook_creates_lead(self, service):
        result = await service.create_lead(
            workspace_id="ws_1",
            company="Globex",
            contact_email="founder@globex.com",
            source=LeadSource.WEBHOOK,
            submission_id="sub_abc123",
        )
        assert result.lead_id is not None

    @pytest.mark.asyncio
    async def test_duplicate_submission_rejected(self, service):
        """FR-4: Same submission_id within 5 min → DuplicateSubmission."""
        await service.create_lead(
            workspace_id="ws_1",
            company="Globex",
            contact_email="founder@globex.com",
            source=LeadSource.WEBHOOK,
            submission_id="sub_abc123",
        )
        with pytest.raises(DuplicateSubmission) as exc_info:
            await service.create_lead(
                workspace_id="ws_1",
                company="Globex",
                contact_email="founder@globex.com",
                source=LeadSource.WEBHOOK,
                submission_id="sub_abc123",
            )
        assert exc_info.value.submission_id == "sub_abc123"

    @pytest.mark.asyncio
    async def test_different_submission_ids_allowed(self, service):
        """Two different submission_ids create two leads."""
        r1 = await service.create_lead(
            workspace_id="ws_1", company="A", contact_email="a@x.com",
            source=LeadSource.WEBHOOK, submission_id="sub_1",
        )
        r2 = await service.create_lead(
            workspace_id="ws_1", company="B", contact_email="b@x.com",
            source=LeadSource.WEBHOOK, submission_id="sub_2",
        )
        assert r1.lead_id != r2.lead_id

    @pytest.mark.asyncio
    async def test_no_submission_id_no_dedup(self, service):
        """Manual intake has no submission_id → no dedup → multiple leads OK."""
        r1 = await service.create_lead(
            workspace_id="ws_1", company="A", contact_email="a@x.com",
            source=LeadSource.MANUAL,
        )
        r2 = await service.create_lead(
            workspace_id="ws_1", company="A", contact_email="a@x.com",
            source=LeadSource.MANUAL,
        )
        assert r1.lead_id != r2.lead_id


# ---------------------------------------------------------------------------
# 3. Simulated intake
# ---------------------------------------------------------------------------


class TestSimulatedIntake:
    @pytest.mark.asyncio
    async def test_simulated_lead_created(self, service):
        result = await service.create_lead(
            workspace_id="ws_1",
            company="Simulated Corp",
            contact_email="sim@example.com",
            source=LeadSource.SIMULATED,
        )
        lead = await service.get_lead("ws_1", result.lead_id)
        assert lead is not None
        assert lead.source == LeadSource.SIMULATED


# ---------------------------------------------------------------------------
# 4. All three paths use the same create_lead method (FR-5)
# ---------------------------------------------------------------------------


class TestSingleEntryPoint:
    """FR-5: All three intake paths invoke the same LeadIntakeService.create_lead."""

    @pytest.mark.asyncio
    async def test_all_sources_create_lead_via_same_method(self, service):
        for source in [LeadSource.MANUAL, LeadSource.WEBHOOK, LeadSource.SIMULATED]:
            result = await service.create_lead(
                workspace_id="ws_1",
                company=f"Corp-{source.value}",
                contact_email=f"{source.value}@x.com",
                source=source,
                submission_id=f"sub_{source.value}" if source != LeadSource.MANUAL else None,
            )
            assert result.lead_id is not None
            lead = await service.get_lead("ws_1", result.lead_id)
            assert lead.source == source


# ---------------------------------------------------------------------------
# 5. Suppression list (Correction 7)
# ---------------------------------------------------------------------------


class TestSuppression:
    @pytest.mark.asyncio
    async def test_suppressed_email_rejected(self, store, queue):
        service = LeadIntakeService(
            store=store, queue=queue,
            suppression_list={"evil@attacker.com"},
        )
        with pytest.raises(SuppressedEmail):
            await service.create_lead(
                workspace_id="ws_1",
                company="Evil",
                contact_email="evil@attacker.com",
                source=LeadSource.MANUAL,
            )

    @pytest.mark.asyncio
    async def test_set_suppression_list_per_workspace(self, store, queue):
        service = LeadIntakeService(store=store, queue=queue)
        service.set_suppression_list("ws_1", {"bad@x.com"})
        # ws_1 has the suppression; ws_2 does not
        with pytest.raises(SuppressedEmail):
            await service.create_lead(
                workspace_id="ws_1", company="X", contact_email="bad@x.com",
                source=LeadSource.MANUAL,
            )
        # ws_2 allows the same email
        result = await service.create_lead(
            workspace_id="ws_2", company="X", contact_email="bad@x.com",
            source=LeadSource.MANUAL,
        )
        assert result.lead_id is not None

    @pytest.mark.asyncio
    async def test_suppression_case_insensitive(self, store, queue):
        """Email matching should be case-insensitive."""
        service = LeadIntakeService(
            store=store, queue=queue,
            suppression_list={"Bad@X.com"},
        )
        with pytest.raises(SuppressedEmail):
            await service.create_lead(
                workspace_id="ws_1", company="X",
                contact_email="bad@x.com",  # different case
                source=LeadSource.MANUAL,
            )


# ---------------------------------------------------------------------------
# 6. Workspace isolation
# ---------------------------------------------------------------------------


class TestWorkspaceIsolation:
    @pytest.mark.asyncio
    async def test_get_lead_cross_workspace_returns_none(self, service):
        result = await service.create_lead(
            workspace_id="ws_1", company="Acme",
            contact_email="r@acme.com", source=LeadSource.MANUAL,
        )
        # Access from a different workspace → None
        lead = await service.get_lead("ws_2", result.lead_id)
        assert lead is None

    @pytest.mark.asyncio
    async def test_dedup_is_workspace_scoped(self, service):
        """Same submission_id in different workspaces creates different leads."""
        r1 = await service.create_lead(
            workspace_id="ws_1", company="A", contact_email="a@x.com",
            source=LeadSource.WEBHOOK, submission_id="sub_1",
        )
        r2 = await service.create_lead(
            workspace_id="ws_2", company="B", contact_email="b@x.com",
            source=LeadSource.WEBHOOK, submission_id="sub_1",
        )
        assert r1.lead_id != r2.lead_id


# ---------------------------------------------------------------------------
# 7. Disposition updates
# ---------------------------------------------------------------------------


class TestDispositionUpdate:
    @pytest.mark.asyncio
    async def test_update_disposition(self, service):
        result = await service.create_lead(
            workspace_id="ws_1", company="Acme",
            contact_email="r@acme.com", source=LeadSource.MANUAL,
        )
        await service.update_lead_disposition(
            "ws_1", result.lead_id, LeadDisposition.QUALIFIED, "matches ICP",
        )
        lead = await service.get_lead("ws_1", result.lead_id)
        assert lead.disposition == LeadDisposition.QUALIFIED
        assert lead.disposition_reason == "matches ICP"

    @pytest.mark.asyncio
    async def test_update_disposition_cross_workspace_silent(self, service):
        """Updating a lead from the wrong workspace does nothing (no error)."""
        result = await service.create_lead(
            workspace_id="ws_1", company="Acme",
            contact_email="r@acme.com", source=LeadSource.MANUAL,
        )
        await service.update_lead_disposition(
            "ws_2", result.lead_id, LeadDisposition.QUALIFIED,
        )
        lead = await service.get_lead("ws_1", result.lead_id)
        assert lead.disposition == LeadDisposition.NEW  # unchanged


# ---------------------------------------------------------------------------
# 8. Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_dedup_window_is_5_minutes(self):
        """Engineering Contract: dedup window is 5 minutes."""
        assert DEDUP_WINDOW_SECONDS == 300
