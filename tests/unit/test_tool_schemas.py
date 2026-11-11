"""
Tests for opsmesh.tools.schemas.

Verifies:
  - Valid args pass validation
  - Invalid args (bad email, bad lead_id format, out-of-range) raise ValidationError
  - Status enums reject unknown values
  - approval_id pattern is enforced
  - All 5 MCP tool schemas + web_search exist
  - ToolError codes are the documented set
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from opsmesh.tools.schemas import (
    CreateLeadArgs,
    CreateLeadResult,
    LookupLeadArgs,
    LookupLeadResult,
    SendEmailArgs,
    SendEmailResult,
    SendNotificationArgs,
    SendNotificationResult,
    ToolError,
    UpdateLeadStatusArgs,
    UpdateLeadStatusResult,
    WebSearchArgs,
    WebSearchHit,
    WebSearchResult,
)


# ---------------------------------------------------------------------------
# 1. lookup_lead
# ---------------------------------------------------------------------------


class TestLookupLead:
    def test_valid_lead_id(self):
        args = LookupLeadArgs(lead_id="L_412")
        assert args.lead_id == "L_412"

    def test_invalid_lead_id_format_rejected(self):
        with pytest.raises(ValidationError):
            LookupLeadArgs(lead_id="412")  # missing L_ prefix
        with pytest.raises(ValidationError):
            LookupLeadArgs(lead_id="L_abc")  # non-digits
        with pytest.raises(ValidationError):
            LookupLeadArgs(lead_id="")  # empty

    def test_result_has_required_fields(self):
        from opsmesh.models import utcnow
        result = LookupLeadResult(
            lead_id="L_412", company="Acme",
            contact_email="r@acme.com", created_at=utcnow(),
            last_modified_at=utcnow(),
        )
        assert result.company == "Acme"


# ---------------------------------------------------------------------------
# 2. update_lead_status
# ---------------------------------------------------------------------------


class TestUpdateLeadStatus:
    def test_valid_status(self):
        for status in ["contacted", "qualified", "rejected", "disqualified", "needs_review"]:
            args = UpdateLeadStatusArgs(lead_id="L_1", status=status)
            assert args.status == status

    def test_invalid_status_rejected(self):
        """Defense in depth: even if the LLM tries status='admin', Pydantic
        rejects before the ToolGateway sees it (Day 4 adds gateway check)."""
        with pytest.raises(ValidationError):
            UpdateLeadStatusArgs(lead_id="L_1", status="admin")  # type: ignore

    def test_result_records_previous_status(self):
        result = UpdateLeadStatusResult(
            lead_id="L_1", previous_status="new", new_status="contacted",
        )
        assert result.previous_status == "new"
        assert result.new_status == "contacted"


# ---------------------------------------------------------------------------
# 3. create_lead
# ---------------------------------------------------------------------------


class TestCreateLead:
    def test_valid_args(self):
        args = CreateLeadArgs(
            company="Acme Corp",
            contact_email="founder@acme.com",
            industry="Software",
            employees=25,
        )
        assert args.source == "manual"  # default

    def test_empty_company_rejected(self):
        with pytest.raises(ValidationError):
            CreateLeadArgs(company="", contact_email="r@acme.com")

    def test_invalid_email_rejected(self):
        with pytest.raises(ValidationError):
            CreateLeadArgs(company="Acme", contact_email="not-an-email")

    def test_employees_range(self):
        with pytest.raises(ValidationError):
            CreateLeadArgs(company="Acme", contact_email="r@acme.com", employees=0)
        with pytest.raises(ValidationError):
            CreateLeadArgs(
                company="Acme", contact_email="r@acme.com", employees=2_000_001,
            )

    def test_result_generates_lead_id(self):
        result = CreateLeadResult()
        assert result.lead_id.startswith("L_")
        assert len(result.lead_id) == 8  # L_ + 6 digits


# ---------------------------------------------------------------------------
# 4. send_email
# ---------------------------------------------------------------------------


class TestSendEmail:
    def test_valid_args(self):
        args = SendEmailArgs(
            to="founder@acme.com",
            from_="Bekalu <bekalu@opsmesh.dev>",
            subject="Hello",
            body="Body text",
            approval_id="ap_abcdef123456",
        )
        assert args.to == "founder@acme.com"

    def test_approval_id_pattern_required(self):
        """approval_id must match ap_<12 hex chars>."""
        with pytest.raises(ValidationError):
            SendEmailArgs(
                to="r@acme.com", from_="s@x.com", subject="x", body="x",
                approval_id="bad_id",
            )
        with pytest.raises(ValidationError):
            SendEmailArgs(
                to="r@acme.com", from_="s@x.com", subject="x", body="x",
                approval_id="ap_short",
            )

    def test_subject_length_limit(self):
        with pytest.raises(ValidationError):
            SendEmailArgs(
                to="r@acme.com", from_="s@x.com", subject="x" * 201,
                body="x", approval_id="ap_abcdef123456",
            )

    def test_body_length_limit(self):
        with pytest.raises(ValidationError):
            SendEmailArgs(
                to="r@acme.com", from_="s@x.com", subject="x",
                body="x" * 2001, approval_id="ap_abcdef123456",
            )

    def test_result_status_enum(self):
        for status in ["sent", "queued", "failed"]:
            result = SendEmailResult(status=status)  # type: ignore
            assert result.status == status
        with pytest.raises(ValidationError):
            SendEmailResult(status="maybe")  # type: ignore


# ---------------------------------------------------------------------------
# 5. send_notification
# ---------------------------------------------------------------------------


class TestSendNotification:
    def test_valid_args(self):
        args = SendNotificationArgs(
            subject="Approval requested",
            body="Please review run X",
            kind="approval_requested",
        )
        assert args.to == "workspace_owner"

    def test_invalid_kind_rejected(self):
        with pytest.raises(ValidationError):
            SendNotificationArgs(
                subject="x", body="x", kind="invalid_kind",  # type: ignore
            )

    def test_recipient_is_workspace_owner_only(self):
        """v1 only allows 'workspace_owner' as recipient — no arbitrary to."""
        args = SendNotificationArgs(subject="x", body="x", kind="run_failed")
        assert args.to == "workspace_owner"


# ---------------------------------------------------------------------------
# 6. web_search (inline — not an MCP tool per Correction 9)
# ---------------------------------------------------------------------------


class TestWebSearch:
    def test_valid_args(self):
        args = WebSearchArgs(query="acme corp funding", max_results=5)
        assert args.max_results == 5

    def test_empty_query_rejected(self):
        with pytest.raises(ValidationError):
            WebSearchArgs(query="")

    def test_max_results_range(self):
        with pytest.raises(ValidationError):
            WebSearchArgs(query="x", max_results=0)
        with pytest.raises(ValidationError):
            WebSearchArgs(query="x", max_results=11)

    def test_result_with_hits(self):
        from pydantic import HttpUrl
        hit = WebSearchHit(url="https://acme.com", title="Acme", snippet="...")
        result = WebSearchResult(results=[hit])
        assert len(result.results) == 1


# ---------------------------------------------------------------------------
# 7. ToolError — standard error model
# ---------------------------------------------------------------------------


class TestToolError:
    def test_valid_codes(self):
        for code in ["not_found", "forbidden", "rate_limited", "provider_timeout"]:
            err = ToolError(code=code, message="x")  # type: ignore
            assert err.code == code

    def test_invalid_code_rejected(self):
        with pytest.raises(ValidationError):
            ToolError(code="unknown_error", message="x")  # type: ignore

    def test_retry_after_optional(self):
        err = ToolError(code="rate_limited", message="x", retry_after_seconds=30)
        assert err.retry_after_seconds == 30
        err2 = ToolError(code="not_found", message="x")
        assert err2.retry_after_seconds is None
