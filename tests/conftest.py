"""
Shared test fixtures.

Provides:
    - force_test_defaults (autouse): disables .env file loading + forces
      mock providers + in-memory DB. This prevents the user's .env file
      (which may have real API keys and database_path set) from breaking
      tests.
    - fresh_store: a new InMemoryStateStore per test (full isolation)
    - sample workspace/lead IDs (deterministic, readable)
    - sample_run: a RunRecord at state=NEW
    - Mock specialists WITH spies (call counters) to prove non-repetition
    - Fixtures for: insufficient research, NEEDS_REVIEW, critic rejection,
      max attempts, malformed result, unexpected exception
"""

from __future__ import annotations

import os

import pytest

from opsmesh.config import Settings, get_settings
from opsmesh.models import (
    CriticResult,
    DraftResult,
    QualificationResult,
    ResearchResult,
    RunRecord,
)
from opsmesh.state_machine import State
from opsmesh.state_store import InMemoryStateStore


# ---------------------------------------------------------------------------
# GLOBAL: force mock providers + in-memory DB for ALL tests
#
# WHY: On the user's machine, they have a .env file with real API keys
# (Groq, Exa, Resend, HubSpot) and OPSMESH_DATABASE_PATH=./opsmesh.db.
# Without this fixture, pydantic-settings loads .env on every test,
# causing:
#   1. Settings().llm_provider == "groq" (not "mock") → assertion failures
#   2. _db_backend == "sqlite" → SQLiteStateStore is used → but init_db()
#      only runs in FastAPI's lifespan handler → TestClient(app) without
#      `with` doesn't trigger lifespan → "no such table: runs" error
#
# This fixture:
#   1. Disables .env file loading (sets env_file=None in model_config)
#   2. Clears ALL OPSMESH_ env vars (so code defaults are used)
#   3. Resets DB state (so _db_backend is "memory")
#   4. Clears settings cache (so next get_settings() re-reads)
#
# Per-test-file fixtures can still OVERRIDE these by setting their own
# env vars (e.g., auth tests set OPSMESH_GOOGLE_OAUTH_CLIENT_ID).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def force_test_defaults(monkeypatch):
    """Force all tests to use mock providers + in-memory DB.

    This runs BEFORE every test, regardless of which test file it's in.
    It prevents the user's .env file from leaking real API keys and
    database_path into the test environment.
    """
    # 1. Disable .env file loading for the Settings class
    #    (pydantic-settings reads model_config.env_file at instantiation time)
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    # 2. Clear ALL OPSMESH_ env vars (so code defaults are used, not .env)
    for key in list(os.environ.keys()):
        if key.startswith("OPSMESH_"):
            monkeypatch.delenv(key, raising=False)

    # 3. Reset DB state (module-level globals in connection.py)
    from opsmesh.adapters.db.connection import reset_db_state
    reset_db_state()

    # 4. Clear settings cache
    get_settings.cache_clear()

    yield

    # Cleanup after test
    get_settings.cache_clear()
    reset_db_state()


@pytest.fixture
def fresh_store() -> InMemoryStateStore:
    """A fresh in-memory store, empty. One per test — full isolation."""
    return InMemoryStateStore()


@pytest.fixture
def sample_workspace_id() -> str:
    return "ws_test_001"


@pytest.fixture
def other_workspace_id() -> str:
    """A different workspace — for cross-workspace isolation tests."""
    return "ws_other_999"


@pytest.fixture
def sample_lead_id() -> str:
    return "lead_abc123"


@pytest.fixture
def sample_run(sample_workspace_id: str, sample_lead_id: str) -> RunRecord:
    """A RunRecord at state=NEW, ready for the supervisor to process."""
    return RunRecord(
        workspace_id=sample_workspace_id,
        lead_id=sample_lead_id,
        state=State.NEW,
    )


# ---------------------------------------------------------------------------
# Spying mock specialists — track call counts to prove non-repetition.
# ---------------------------------------------------------------------------


class SpySpecialist:
    """
    Base class for spying mock specialists. Tracks:
        - call_count: how many times this specialist was invoked.
        - calls: list of (run_id, step_id) for each invocation.

    This lets tests assert "research was NOT re-executed on resume."
    """

    def __init__(self):
        self.call_count = 0
        self.calls: list[tuple[str, int]] = []

    async def _record_call(self, run: RunRecord) -> None:
        self.call_count += 1
        self.calls.append((run.run_id, run.step_id))

    @property
    def was_called(self) -> bool:
        return self.call_count > 0


class SpyResearchSpecialist(SpySpecialist):
    """Returns sufficient research (QUALIFIED path)."""

    def __init__(self, sufficient: bool = True):
        super().__init__()
        self._sufficient = sufficient

    async def __call__(self, run, budget=None, ctx=None):
        await self._record_call(run)
        return ResearchResult(
            evidence=[],
            missing_info=[] if self._sufficient else ["company_website"],
            summary="Mock research summary.",
            sufficient_for_qualification=self._sufficient,
        )


class SpyQualifySpecialist(SpySpecialist):
    """Returns QUALIFIED by default; configurable for other statuses."""

    def __init__(self, status: str = "QUALIFIED", score: float = 0.85):
        super().__init__()
        self._status = status
        self._score = score

    async def __call__(self, run, budget=None, ctx=None):
        await self._record_call(run)
        return QualificationResult(
            status=self._status,
            score=self._score,
            reasoning="Mock qualification.",
            matched_criteria=["industry=Software"] if self._status == "QUALIFIED" else [],
            unmatched_criteria=[],
            risk_flags=[],
        )


class SpyDraftSpecialist(SpySpecialist):
    async def __call__(self, run, budget=None, ctx=None):
        await self._record_call(run)
        return DraftResult(
            subject="Mock subject",
            body="Mock body.",
            recipient="founder@example.com",
            claims=[],
        )


class SpyCriticSpecialist(SpySpecialist):
    """Returns APPROVED by default; configurable for REJECTED."""

    def __init__(self, verdict: str = "APPROVED", feedback: str = ""):
        super().__init__()
        self._verdict = verdict
        self._feedback = feedback

    async def __call__(self, run, budget=None, ctx=None):
        await self._record_call(run)
        return CriticResult(
            verdict=self._verdict,
            feedback=self._feedback,
            issues=[],
        )


class MalformedSpecialist(SpySpecialist):
    """Returns an object without the expected attribute (e.g., no verdict)."""

    async def __call__(self, run, budget=None, ctx=None):
        await self._record_call(run)
        # Return an object with NO verdict attribute
        return type("Malformed", (), {})()


class ThrowingSpecialist(SpySpecialist):
    """Raises an exception to test error handling."""

    def __init__(self, exception: Exception):
        super().__init__()
        self._exception = exception

    async def __call__(self, run, budget=None, ctx=None):
        await self._record_call(run)
        raise self._exception


# ---------------------------------------------------------------------------
# Fixture: default mock specialists (happy path)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_specialists():
    """Dict of mock specialists that produce the happy path."""
    return {
        "researching": SpyResearchSpecialist(sufficient=True),
        "qualifying": SpyQualifySpecialist(status="QUALIFIED"),
        "drafting": SpyDraftSpecialist(),
        "critic": SpyCriticSpecialist(verdict="APPROVED"),
    }


# ---------------------------------------------------------------------------
# Fixture: insufficient research (Gate 1 path)
# ---------------------------------------------------------------------------


@pytest.fixture
def insufficient_research_specialists():
    """Mock specialists where research returns insufficient_for_qualification=False.
    The run should pause at AWAITING_EVIDENCE_REVIEW (Gate 1)."""
    return {
        "researching": SpyResearchSpecialist(sufficient=False),
        "qualifying": SpyQualifySpecialist(status="QUALIFIED"),
        "drafting": SpyDraftSpecialist(),
        "critic": SpyCriticSpecialist(verdict="APPROVED"),
    }


# ---------------------------------------------------------------------------
# Fixture: NEEDS_REVIEW qualification (Gate 1 path)
# ---------------------------------------------------------------------------


@pytest.fixture
def needs_review_specialists():
    """Mock specialists where qualification returns NEEDS_REVIEW.
    The run should pause at AWAITING_EVIDENCE_REVIEW (Gate 1)."""
    return {
        "researching": SpyResearchSpecialist(sufficient=True),
        "qualifying": SpyQualifySpecialist(status="NEEDS_REVIEW", score=0.5),
        "drafting": SpyDraftSpecialist(),
        "critic": SpyCriticSpecialist(verdict="APPROVED"),
    }


# ---------------------------------------------------------------------------
# Fixture: disqualification (short-circuit to FAILED)
# ---------------------------------------------------------------------------


@pytest.fixture
def disqualified_specialists():
    return {
        "researching": SpyResearchSpecialist(sufficient=True),
        "qualifying": SpyQualifySpecialist(status="DISQUALIFIED", score=0.1),
        "drafting": SpyDraftSpecialist(),
        "critic": SpyCriticSpecialist(verdict="APPROVED"),
    }


# ---------------------------------------------------------------------------
# Fixture: critic rejection (retry drafting)
# ---------------------------------------------------------------------------


@pytest.fixture
def critic_reject_specialists():
    """Critic always rejects — used to test max draft attempts."""
    return {
        "researching": SpyResearchSpecialist(sufficient=True),
        "qualifying": SpyQualifySpecialist(status="QUALIFIED"),
        "drafting": SpyDraftSpecialist(),
        "critic": SpyCriticSpecialist(verdict="REJECTED", feedback="Improve the opening."),
    }


# ---------------------------------------------------------------------------
# Fixture: malformed critic result (no verdict attribute)
# ---------------------------------------------------------------------------


@pytest.fixture
def malformed_critic_specialists():
    return {
        "researching": SpyResearchSpecialist(sufficient=True),
        "qualifying": SpyQualifySpecialist(status="QUALIFIED"),
        "drafting": SpyDraftSpecialist(),
        "critic": MalformedSpecialist(),
    }


# ---------------------------------------------------------------------------
# Fixture: throwing specialist (exception handling)
# ---------------------------------------------------------------------------


@pytest.fixture
def throwing_specialists():
    return {
        "researching": ThrowingSpecialist(RuntimeError("Simulated LLM timeout")),
        "qualifying": SpyQualifySpecialist(status="QUALIFIED"),
        "drafting": SpyDraftSpecialist(),
        "critic": SpyCriticSpecialist(verdict="APPROVED"),
    }


# ---------------------------------------------------------------------------
# Mock job
# ---------------------------------------------------------------------------


class MockJob:
    """Minimal job object for supervisor.run()."""

    def __init__(self, run_id: str, workspace_id: str, lead_id: str):
        self.run_id = run_id
        self.workspace_id = workspace_id
        self.lead_id = lead_id


@pytest.fixture
def make_job():
    """Factory for creating MockJob objects."""
    def _make(run: RunRecord) -> MockJob:
        return MockJob(
            run_id=run.run_id,
            workspace_id=run.workspace_id,
            lead_id=run.lead_id,
        )
    return _make
