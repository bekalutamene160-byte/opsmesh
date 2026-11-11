"""
Tests for opsmesh.models.

Covers:
    - Valid and invalid result models (Pydantic validation)
    - Enum representation (use_enum_values)
    - Counter constraints (ge=0, ge=1, max)
    - Evidence-reference integrity (Claim.evidence_id)
    - Invalid critic verdicts rejected
    - Timezone-aware timestamps
    - LeadDisposition enum
    - CRMOutboxStatus enum
    - IdempotencyRecord typed model
    - Lead and ContactPolicy models
"""

from __future__ import annotations

from datetime import timezone

import pytest
from pydantic import ValidationError

from opsmesh.models import (
    Claim,
    ContactPolicy,
    CRMOutboxOp,
    CRMOutboxOperation,
    CRMOutboxStatus,
    CriticIssue,
    CriticResult,
    DraftResult,
    EvidenceItem,
    IdempotencyRecord,
    IdempotencyStatus,
    Lead,
    LeadDisposition,
    LeadSource,
    QualificationResult,
    ResearchResult,
    RunRecord,
    utcnow,
)
from opsmesh.state_machine import State


# ---------------------------------------------------------------------------
# 1. RunRecord basics
# ---------------------------------------------------------------------------


class TestRunRecord:
    def test_new_run_has_defaults(self):
        run = RunRecord(workspace_id="ws_1", lead_id="lead_1")
        assert run.state == State.NEW
        assert run.step_id == 0
        assert run.version == 0
        assert run.draft_attempts == 0
        assert run.research is None
        assert run.parent_run_id is None

    def test_run_id_is_generated(self):
        run = RunRecord(workspace_id="ws_1", lead_id="lead_1")
        assert run.run_id.startswith("run_")

    def test_parent_run_id_for_retry(self):
        """Correction 4: retries link to parent run."""
        run = RunRecord(
            workspace_id="ws_1", lead_id="lead_1",
            parent_run_id="run_original_abc",
        )
        assert run.parent_run_id == "run_original_abc"

    def test_version_starts_at_zero(self):
        """Correction 14: version field for CAS."""
        run = RunRecord(workspace_id="ws_1", lead_id="lead_1")
        assert run.version == 0

    def test_increment_version(self):
        run = RunRecord(workspace_id="ws_1", lead_id="lead_1")
        v1 = run.increment_version()
        assert v1 == 1
        assert run.version == 1
        v2 = run.increment_version()
        assert v2 == 2

    def test_step_id_must_be_non_negative(self):
        with pytest.raises(ValidationError):
            RunRecord(workspace_id="ws_1", lead_id="lead_1", step_id=-1)

    def test_budget_constraints(self):
        """Budget fields must have valid constraints."""
        with pytest.raises(ValidationError):
            RunRecord(workspace_id="ws_1", lead_id="lead_1", budget_max_tokens=0)
        with pytest.raises(ValidationError):
            RunRecord(workspace_id="ws_1", lead_id="lead_1", budget_max_cost_usd=-1.0)
        with pytest.raises(ValidationError):
            RunRecord(workspace_id="ws_1", lead_id="lead_1", budget_max_seconds=0)

    def test_is_terminal(self):
        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.SENT)
        assert run.is_terminal() is True
        run.state = State.NEW
        assert run.is_terminal() is False

    def test_is_paused(self):
        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.AWAITING_APPROVAL)
        assert run.is_paused() is True
        run.state = State.RESEARCHING
        assert run.is_paused() is False

    def test_timestamps_are_timezone_aware(self):
        """All datetimes must be UTC, timezone-aware."""
        run = RunRecord(workspace_id="ws_1", lead_id="lead_1")
        assert run.started_at.tzinfo is not None
        assert run.updated_at.tzinfo is not None

    def test_state_coerced_from_string(self):
        """When loaded from JSON, state string should coerce to State enum."""
        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state="researching")  # type: ignore
        assert run.state == State.RESEARCHING

    def test_json_serialization_preserves_state(self):
        """use_enum_values=True means state serializes as 'researching'."""
        run = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.RESEARCHING)
        data = run.model_dump_json()
        assert '"researching"' in data
        # Reload
        run2 = RunRecord.model_validate_json(data)
        assert run2.state == State.RESEARCHING


# ---------------------------------------------------------------------------
# 2. Specialist result models
# ---------------------------------------------------------------------------


class TestEvidenceItem:
    def test_valid_evidence(self):
        ev = EvidenceItem(source="web", content="Acme Corp was founded in 2020.")
        assert ev.id.startswith("ev_")
        assert ev.confidence == "medium"

    def test_empty_content_rejected(self):
        with pytest.raises(ValidationError):
            EvidenceItem(source="web", content="")

    def test_invalid_source_rejected(self):
        with pytest.raises(ValidationError):
            EvidenceItem(source="invalid", content="content")  # type: ignore

    def test_invalid_confidence_rejected(self):
        with pytest.raises(ValidationError):
            EvidenceItem(source="web", content="content", confidence="invalid")  # type: ignore


class TestResearchResult:
    def test_valid_research(self):
        r = ResearchResult(
            evidence=[],
            summary="Research summary.",
            sufficient_for_qualification=True,
        )
        assert r.sufficient_for_qualification is True

    def test_empty_summary_rejected(self):
        with pytest.raises(ValidationError):
            ResearchResult(summary="", sufficient_for_qualification=True)

    def test_missing_sufficient_flag_rejected(self):
        with pytest.raises(ValidationError):
            ResearchResult(summary="ok")  # type: ignore


class TestQualificationResult:
    def test_valid_qualified(self):
        q = QualificationResult(
            status="QUALIFIED", score=0.9, reasoning="Matches ICP."
        )
        assert q.status == "QUALIFIED"

    def test_score_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            QualificationResult(status="QUALIFIED", score=1.5, reasoning="ok")
        with pytest.raises(ValidationError):
            QualificationResult(status="QUALIFIED", score=-0.1, reasoning="ok")

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            QualificationResult(status="UNKNOWN", score=0.5, reasoning="ok")  # type: ignore

    def test_empty_reasoning_rejected(self):
        with pytest.raises(ValidationError):
            QualificationResult(status="QUALIFIED", score=0.5, reasoning="")


class TestDraftResult:
    def test_valid_draft(self):
        d = DraftResult(
            subject="Subject",
            body="Body text.",
            recipient="founder@example.com",
            claims=[],
        )
        assert d.subject == "Subject"

    def test_subject_too_long_rejected(self):
        with pytest.raises(ValidationError):
            DraftResult(subject="x" * 201, body="body", recipient="r@e.com")

    def test_claim_has_evidence_id(self):
        """Claims must cite an evidence_id (validated at supervisor level)."""
        c = Claim(text="Acme is hiring", evidence_id="ev_001")
        assert c.evidence_id == "ev_001"

    def test_empty_claim_text_rejected(self):
        with pytest.raises(ValidationError):
            Claim(text="", evidence_id="ev_001")


class TestCriticResult:
    def test_valid_approved(self):
        c = CriticResult(verdict="APPROVED")
        assert c.verdict == "APPROVED"

    def test_valid_rejected_with_feedback(self):
        c = CriticResult(verdict="REJECTED", feedback="Improve opening.")
        assert c.verdict == "REJECTED"
        assert c.feedback == "Improve opening."

    def test_invalid_verdict_rejected(self):
        """Only APPROVED and REJECTED are valid verdicts.
        Unknown verdicts are rejected at the model level (not just the
        supervisor level) — this is defense in depth."""
        with pytest.raises(ValidationError):
            CriticResult(verdict="MAYBE")  # type: ignore

    def test_critic_issue_valid_kinds(self):
        issue = CriticIssue(kind="ungrounded_claim", detail="Claim X has no citation.")
        assert issue.kind == "ungrounded_claim"

    def test_critic_issue_invalid_kind_rejected(self):
        with pytest.raises(ValidationError):
            CriticIssue(kind="invalid_kind", detail="x")  # type: ignore


# ---------------------------------------------------------------------------
# 3. Lead and disposition (Correction 6)
# ---------------------------------------------------------------------------


class TestLead:
    def test_valid_lead(self):
        lead = Lead(
            workspace_id="ws_1",
            company="Acme Corp",
            contact_email="founder@acme.com",
            source=LeadSource.MANUAL,
        )
        assert lead.disposition == LeadDisposition.NEW

    def test_scraped_source_not_available(self):
        """Correction 7: 'scraped' is not a valid lead source."""
        assert not hasattr(LeadSource, "SCRAPED")

    def test_empty_company_rejected(self):
        with pytest.raises(ValidationError):
            Lead(
                workspace_id="ws_1", company="",
                contact_email="r@e.com", source=LeadSource.MANUAL,
            )

    def test_employees_range_constraint(self):
        with pytest.raises(ValidationError):
            Lead(
                workspace_id="ws_1", company="Acme",
                contact_email="r@e.com", source=LeadSource.MANUAL,
                employees=0,  # must be >= 1
            )
        with pytest.raises(ValidationError):
            Lead(
                workspace_id="ws_1", company="Acme",
                contact_email="r@e.com", source=LeadSource.MANUAL,
                employees=2_000_000,  # must be <= 1_000_000
            )


class TestContactPolicy:
    def test_default_policy(self):
        policy = ContactPolicy(
            workspace_id="ws_1",
            sender_identity="Bekalu <bekalu@opsmesh.dev>",
        )
        assert LeadSource.MANUAL in policy.allowed_sources
        assert LeadSource.WEBHOOK in policy.allowed_sources
        assert policy.suppression_list == []
        assert policy.requires_explicit_consent is True

    def test_suppression_list(self):
        policy = ContactPolicy(
            workspace_id="ws_1",
            sender_identity="Bekalu <bekalu@opsmesh.dev>",
            suppression_list=["evil@attacker.com"],
        )
        assert "evil@attacker.com" in policy.suppression_list


# ---------------------------------------------------------------------------
# 4. CRM outbox (Correction 2)
# ---------------------------------------------------------------------------


class TestCRMOutbox:
    def test_outbox_op_defaults(self):
        op = CRMOutboxOp(
            workspace_id="ws_1",
            run_id="run_1",
            lead_id="lead_1",
            operation=CRMOutboxOperation.UPDATE_LEAD_STATUS,
            args={"status": "contacted"},
        )
        assert op.status == CRMOutboxStatus.PENDING
        assert op.attempts == 0
        assert op.max_attempts == 5

    def test_outbox_status_independent_of_run_state(self):
        """Correction 2: SENT does not mean outbox is DONE."""
        op = CRMOutboxOp(
            workspace_id="ws_1", run_id="run_1", lead_id="lead_1",
            operation=CRMOutboxOperation.UPDATE_LEAD_STATUS,
            args={},
            status=CRMOutboxStatus.PENDING,  # still pending even if run is SENT
        )
        assert op.status == CRMOutboxStatus.PENDING

    def test_invalid_operation_rejected(self):
        with pytest.raises(ValidationError):
            CRMOutboxOp(
                workspace_id="ws_1", run_id="run_1", lead_id="lead_1",
                operation="invalid",  # type: ignore
                args={},
            )


# ---------------------------------------------------------------------------
# 5. IdempotencyRecord (Correction 19)
# ---------------------------------------------------------------------------


class TestIdempotencyRecord:
    def test_valid_record(self):
        rec = IdempotencyRecord(
            key="ik_abc123",
            workspace_id="ws_1",
            run_id="run_1",
            step_id=3,
            tool_name="send_email",
        )
        assert rec.status == IdempotencyStatus.PENDING
        assert rec.result is None

    def test_step_id_must_be_non_negative(self):
        with pytest.raises(ValidationError):
            IdempotencyRecord(
                key="ik_1", workspace_id="ws_1", run_id="run_1",
                step_id=-1, tool_name="send_email",
            )

    def test_status_can_be_updated(self):
        rec = IdempotencyRecord(
            key="ik_1", workspace_id="ws_1", run_id="run_1",
            step_id=1, tool_name="send_email",
        )
        rec.status = IdempotencyStatus.DONE
        rec.result = {"message_id": "msg_001"}
        assert rec.status == IdempotencyStatus.DONE
        assert rec.result["message_id"] == "msg_001"


# ---------------------------------------------------------------------------
# 6. utcnow helper
# ---------------------------------------------------------------------------


class TestUtcnow:
    def test_utcnow_is_timezone_aware(self):
        now = utcnow()
        assert now.tzinfo is not None
        assert now.tzinfo == timezone.utc

    def test_utcnow_returns_recent_time(self):
        import time
        t1 = utcnow()
        time.sleep(0.001)
        t2 = utcnow()
        assert t2 > t1
