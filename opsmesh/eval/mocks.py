"""
Mock specialists for the eval runner.

These are self-contained (don't import from tests/) so the eval module
can be used standalone. Each mock is configurable via its constructor
to produce different results for different scenarios.

All mocks are deterministic — same config → same result. This makes the
eval suite reproducible without paid API keys (ADR-008: mock-first).
"""

from __future__ import annotations

from typing import Optional

from opsmesh.models import (
    CriticResult,
    DraftResult,
    QualificationResult,
    ResearchResult,
)


class EvalResearchSpecialist:
    """Returns configurable research results.

    If sufficient=True (default), returns enough evidence for qualification.
    If sufficient=False, returns insufficient_for_qualification=True → Gate 1.
    If timeout=True, raises to simulate an LLM timeout during research
    (the supervisor catches the exception → FAILED)."""

    def __init__(self, sufficient: bool = True, timeout: bool = False):
        self._sufficient = sufficient
        self._timeout = timeout
        self.call_count = 0

    async def __call__(self, run, budget=None, ctx=None):
        self.call_count += 1
        if self._timeout:
            raise TimeoutError("LLM call timed out during research (eval mock)")
        return ResearchResult(
            evidence=[],
            missing_info=[] if self._sufficient else ["company_website"],
            summary="Eval research summary." if self._sufficient else "Insufficient evidence.",
            sufficient_for_qualification=self._sufficient,
        )


class EvalQualifySpecialist:
    """Returns configurable qualification results.

    status can be QUALIFIED, NEEDS_REVIEW, or DISQUALIFIED."""

    def __init__(self, status: str = "QUALIFIED", score: float = 0.85):
        self._status = status
        self._score = score
        self.call_count = 0

    async def __call__(self, run, budget=None, ctx=None):
        self.call_count += 1
        return QualificationResult(
            status=self._status,
            score=self._score,
            reasoning=f"Eval qualification: {self._status}.",
            matched_criteria=["industry=Software"] if self._status == "QUALIFIED" else [],
            unmatched_criteria=[],
            risk_flags=[],
        )


class EvalDraftSpecialist:
    """Returns a configurable draft."""

    def __init__(self, subject: str = "Eval subject", body: str = "Eval body."):
        self._subject = subject
        self._body = body
        self.call_count = 0

    async def __call__(self, run, budget=None, ctx=None):
        self.call_count += 1
        return DraftResult(
            subject=self._subject,
            body=self._body,
            recipient="founder@example.com",
            claims=[],
        )


class EvalCriticSpecialist:
    """Returns configurable critic verdicts.

    verdict can be APPROVED or REJECTED."""

    def __init__(self, verdict: str = "APPROVED", feedback: str = ""):
        self._verdict = verdict
        self._feedback = feedback
        self.call_count = 0

    async def __call__(self, run, budget=None, ctx=None):
        self.call_count += 1
        # APPROVED_AFTER_REVISION: reject the first draft, approve the second.
        # This lets eval_013 exercise the critic-retry loop.
        if self._verdict == "APPROVED_AFTER_REVISION":
            if self.call_count == 1:
                return CriticResult(verdict="REJECTED", feedback=self._feedback, issues=[])
            return CriticResult(verdict="APPROVED", feedback="", issues=[])
        return CriticResult(
            verdict=self._verdict,
            feedback=self._feedback,
            issues=[],
        )


class EvalSendingSpecialist:
    """Mock sending specialist — returns sent/unknown/failed per config.

    Config flags (read from the scenario's mock_config):
        email_timeout: True  → {"status": "unknown"}  (SEND_UNKNOWN)
        email_failure: True  → {"status": "failed"}   (FAILED)
        default              → {"status": "sent"}     (→ CRM_UPDATING)
    """

    def __init__(self, timeout: bool = False, failure: bool = False):
        self._timeout = timeout
        self._failure = failure
        self.call_count = 0

    async def __call__(self, run, budget=None, ctx=None):
        self.call_count += 1
        if self._timeout:
            return {"status": "unknown", "error": "Send timed out (eval mock)"}
        if self._failure:
            return {"status": "failed", "error": "Resend returned 4xx (eval mock)"}
        return {"status": "sent", "message_id": "msg_eval_001"}


class EvalCRMUpdatingSpecialist:
    """Mock CRM specialist — non-blocking per the supervisor contract.

    Config flag:
        crm_failure: True → {"status": "failed"} (supervisor still goes to SENT)
        default            → {"status": "synced"}
    """

    def __init__(self, failure: bool = False):
        self._failure = failure
        self.call_count = 0

    async def __call__(self, run, budget=None, ctx=None):
        self.call_count += 1
        if self._failure:
            return {"status": "failed", "error": "HubSpot create_lead failed (eval mock)"}
        return {"status": "synced", "crm_lead_id": "crm_eval_001"}


def make_eval_specialists(config: dict) -> dict:
    """Create a dict of mock specialists from a scenario config.

    Config fields (all optional, with defaults for the happy path):
        research_sufficient: bool (default True)
        qualification_status: "QUALIFIED" | "NEEDS_REVIEW" | "DISQUALIFIED"
        qualification_score: float
        critic_verdict: "APPROVED" | "REJECTED"
        critic_feedback: str
        email_timeout: bool → sending returns "unknown" (SEND_UNKNOWN)
        email_failure: bool → sending returns "failed"
        crm_failure: bool → CRM returns "failed" (non-blocking, run still SENT)
    """
    return {
        "researching": EvalResearchSpecialist(
            sufficient=config.get("research_sufficient", True),
            timeout=config.get("llm_timeout", False),
        ),
        "qualifying": EvalQualifySpecialist(
            status=config.get("qualification_status", "QUALIFIED"),
            score=config.get("qualification_score", 0.85),
        ),
        "drafting": EvalDraftSpecialist(),
        "critic": EvalCriticSpecialist(
            verdict=config.get("critic_verdict", "APPROVED"),
            feedback=config.get("critic_feedback", ""),
        ),
        "sending": EvalSendingSpecialist(
            timeout=config.get("email_timeout", False),
            failure=config.get("email_failure", False),
        ),
        "crm_updating": EvalCRMUpdatingSpecialist(
            failure=config.get("crm_failure", False),
        ),
    }


__all__ = [
    "EvalResearchSpecialist",
    "EvalQualifySpecialist",
    "EvalDraftSpecialist",
    "EvalCriticSpecialist",
    "make_eval_specialists",
]
