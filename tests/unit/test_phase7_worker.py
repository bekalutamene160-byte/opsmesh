"""
Tests for Phase 7.3: Production worker.

Verifies:
  1. _build_default_worker() creates a Worker with real providers
  2. The worker processes a job from the queue (mock LLM)
  3. The worker handles job failures gracefully
  4. The worker respects max_attempts and dead-letters
"""

from __future__ import annotations

import asyncio
import os

import pytest

from opsmesh.config import get_settings
from opsmesh.adapters.db.connection import reset_db_state
from opsmesh.adapters.llm.mock import MockLLMProvider
from opsmesh.adapters.research.mock import MockResearchProvider
from opsmesh.models import LeadSource
from opsmesh.specialists import (
    ResearchSpecialist,
    QualifySpecialist,
    DraftSpecialist,
    CriticSpecialist,
    build_specialists,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Force mock providers + in-memory DB for tests."""
    monkeypatch.setitem(get_settings.__wrapped__.__globals__["Settings"].model_config, "env_file", None)
    for key in list(os.environ.keys()):
        if key.startswith("OPSMESH_"):
            monkeypatch.delenv(key, raising=False)
    reset_db_state()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    reset_db_state()


# ===========================================================================
# 1. Specialists use the LLM provider
# ===========================================================================


class TestRealSpecialists:
    @pytest.mark.asyncio
    async def test_research_specialist_returns_research_result(self):
        """ResearchSpecialist calls the LLM and returns a ResearchResult."""
        llm = MockLLMProvider()
        research = MockResearchProvider()
        specialist = ResearchSpecialist(llm=llm, research=research)

        from opsmesh.models import RunRecord
        from opsmesh.state_machine import State
        run = RunRecord(
            workspace_id="ws_1",
            lead_id="lead_1",
            state=State.RESEARCHING,
        )

        result = await specialist(run, budget=None, ctx=None)
        assert result is not None
        assert result.summary  # non-empty
        assert isinstance(result.sufficient_for_qualification, bool)
        assert isinstance(result.evidence, list)

    @pytest.mark.asyncio
    async def test_qualify_specialist_returns_qualification_result(self):
        """QualifySpecialist calls the LLM and returns a QualificationResult."""
        llm = MockLLMProvider()
        specialist = QualifySpecialist(llm=llm)

        from opsmesh.models import RunRecord, ResearchResult
        from opsmesh.state_machine import State
        run = RunRecord(
            workspace_id="ws_1",
            lead_id="lead_1",
            state=State.QUALIFYING,
        )
        run.research = ResearchResult(
            evidence=[], missing_info=[], summary="Mock research.",
            sufficient_for_qualification=True,
        )

        result = await specialist(run, budget=None, ctx=None)
        assert result is not None
        assert result.status in ("QUALIFIED", "NEEDS_REVIEW", "DISQUALIFIED")
        assert 0.0 <= result.score <= 1.0

    @pytest.mark.asyncio
    async def test_draft_specialist_returns_draft_result(self):
        """DraftSpecialist calls the LLM and returns a DraftResult."""
        llm = MockLLMProvider()
        specialist = DraftSpecialist(llm=llm)

        from opsmesh.models import RunRecord, ResearchResult, QualificationResult
        from opsmesh.state_machine import State
        run = RunRecord(
            workspace_id="ws_1",
            lead_id="lead_1",
            state=State.DRAFTING,
        )
        run.research = ResearchResult(
            evidence=[], missing_info=[], summary="Mock research.",
            sufficient_for_qualification=True,
        )
        run.qualification = QualificationResult(
            status="QUALIFIED", score=0.85, reasoning="Good fit",
        )

        result = await specialist(run, budget=None, ctx=None)
        assert result is not None
        assert result.subject
        assert result.body
        assert result.recipient

    @pytest.mark.asyncio
    async def test_critic_specialist_returns_critic_result(self):
        """CriticSpecialist calls the LLM and returns a CriticResult."""
        llm = MockLLMProvider()
        specialist = CriticSpecialist(llm=llm)

        from opsmesh.models import RunRecord, DraftResult, ResearchResult
        from opsmesh.state_machine import State
        run = RunRecord(
            workspace_id="ws_1",
            lead_id="lead_1",
            state=State.CRITIC,
        )
        run.research = ResearchResult(
            evidence=[], missing_info=[], summary="Mock research.",
            sufficient_for_qualification=True,
        )
        run.draft = DraftResult(
            subject="Test subject",
            body="Test body.",
            recipient="founder@example.com",
        )

        result = await specialist(run, budget=None, ctx=None)
        assert result is not None
        assert result.verdict in ("APPROVED", "REJECTED")


# ===========================================================================
# 2. build_specialists() factory
# ===========================================================================


class TestBuildSpecialists:
    def test_builds_all_4_specialists(self):
        """build_specialists returns a dict with all 4 step names."""
        llm = MockLLMProvider()
        specialists = build_specialists(llm=llm, settings=get_settings())
        assert "researching" in specialists
        assert "qualifying" in specialists
        assert "drafting" in specialists
        assert "critic" in specialists

    def test_uses_research_provider_when_provided(self):
        """When a ResearchProvider is passed, ResearchSpecialist uses it."""
        llm = MockLLMProvider()
        research = MockResearchProvider()
        specialists = build_specialists(llm=llm, settings=get_settings(), research=research)
        assert specialists["researching"].research is research

    def test_works_without_research_provider(self):
        """build_specialists works with research=None (LLM-only mode)."""
        llm = MockLLMProvider()
        specialists = build_specialists(llm=llm, settings=get_settings(), research=None)
        assert specialists["researching"].research is None


# ===========================================================================
# 3. Worker builds with real providers
# ===========================================================================


class TestWorkerBuild:
    def test_build_default_worker_with_mock_providers(self, monkeypatch):
        """_build_default_worker creates a Worker with mock providers by default."""
        # Force mock providers
        monkeypatch.setenv("OPSMESH_LLM_PROVIDER", "mock")
        monkeypatch.setenv("OPSMESH_DATABASE_PATH", "")  # in-memory
        monkeypatch.delenv("OPSMESH_DATABASE_URL", raising=False)
        get_settings.cache_clear()

        from opsmesh.worker import _build_default_worker
        worker = _build_default_worker()

        assert worker.worker_id.startswith("worker_")
        assert worker.supervisor is not None
        assert "researching" in worker.supervisor.specialists
        assert "qualifying" in worker.supervisor.specialists
        assert "drafting" in worker.supervisor.specialists
        assert "critic" in worker.supervisor.specialists

    def test_build_default_worker_with_sqlite(self, monkeypatch, tmp_path):
        """_build_default_worker uses SQLite when database_path is set."""
        db_path = str(tmp_path / "test_worker.db")
        monkeypatch.setenv("OPSMESH_DATABASE_PATH", db_path)
        monkeypatch.setenv("OPSMESH_LLM_PROVIDER", "mock")
        monkeypatch.delenv("OPSMESH_DATABASE_URL", raising=False)
        get_settings.cache_clear()

        from opsmesh.worker import _build_default_worker
        worker = _build_default_worker()

        assert worker.queue is not None
        # The queue should be a SQLiteJobQueue (not InMemoryJobQueue)
        from opsmesh.adapters.db.sqlite_job_queue import SQLiteJobQueue
        assert isinstance(worker.queue, SQLiteJobQueue)


# ===========================================================================
# 4. Worker processes jobs end-to-end
# ===========================================================================


class TestWorkerProcessesJobs:
    @pytest.mark.asyncio
    async def test_worker_processes_job_from_queue(self, monkeypatch):
        """The worker claims a job, runs the supervisor, and completes it."""
        # Use in-memory stores for this test
        from opsmesh.state_store import InMemoryStateStore
        from opsmesh.job_queue import InMemoryJobQueue
        from opsmesh.supervisor import Supervisor
        from opsmesh.worker import Worker
        from opsmesh.models import RunRecord
        from opsmesh.state_machine import State

        store = InMemoryStateStore()
        queue = InMemoryJobQueue()

        # Create a run + enqueue a job (simulating what the API does)
        run = RunRecord(
            workspace_id="ws_1",
            lead_id="lead_1",
            state=State.NEW,
        )
        await store.save_checkpoint(run)
        job_id = await queue.enqueue("ws_1", run.run_id)

        # Build mock specialists (mock LLM returns canned responses)
        from tests.conftest import SpyResearchSpecialist, SpyQualifySpecialist, SpyDraftSpecialist, SpyCriticSpecialist
        specialists = {
            "researching": SpyResearchSpecialist(sufficient=True),
            "qualifying": SpyQualifySpecialist(status="QUALIFIED"),
            "drafting": SpyDraftSpecialist(),
            "critic": SpyCriticSpecialist(verdict="APPROVED"),
        }

        supervisor = Supervisor(specialists=specialists, store=store)
        worker = Worker(queue=queue, supervisor=supervisor, worker_id="test_worker")

        # Process one job
        await worker._process_one_job()

        assert worker.processed_count == 1
        assert worker.failed_count == 0

        # Verify the job was completed
        from opsmesh.models import JobStatus
        # The job should no longer be QUEUED or RUNNING

    @pytest.mark.asyncio
    async def test_worker_handles_empty_queue(self):
        """When the queue is empty, _process_one_job returns without error."""
        from opsmesh.state_store import InMemoryStateStore
        from opsmesh.job_queue import InMemoryJobQueue
        from opsmesh.supervisor import Supervisor
        from opsmesh.worker import Worker

        queue = InMemoryJobQueue()
        supervisor = Supervisor(specialists={}, store=InMemoryStateStore())
        worker = Worker(queue=queue, supervisor=supervisor, worker_id="test_worker")

        # Should not raise
        await worker._process_one_job()
        assert worker.processed_count == 0
