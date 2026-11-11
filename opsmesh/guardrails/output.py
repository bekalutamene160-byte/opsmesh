"""
OPSMESH Output Guardrails — run BEFORE the LLM output becomes an action.

Four guardrails:
  1. OutputSchemaValidator — validates the LLM output matches the expected
                              Pydantic schema (e.g., DraftResult).
  2. GroundingChecker      — verifies every material claim in a draft cites
                              an evidence_id that exists in the run's evidence.
  3. LengthCap             — enforces max word count on the draft body.
  4. RecipientValidator    — verifies the draft recipient matches the lead's
                              contact email.

All produce GuardrailResult (passed | failed | error). The 3-valued result
prevents the silent bypass bug from Course Case 8.
"""

from __future__ import annotations

from typing import Any, Optional, Type

from pydantic import BaseModel, ValidationError

from opsmesh.guardrails.base import GuardrailResult, GuardrailStatus, OutputGuardrail


# ---------------------------------------------------------------------------
# 1. OutputSchemaValidator
# ---------------------------------------------------------------------------


class OutputSchemaValidator(OutputGuardrail):
    """
    Validates that the LLM output matches the expected Pydantic schema.

    If the LLM returns invalid JSON or a schema mismatch, this guardrail
    catches it BEFORE the output becomes an action (e.g., a draft).

    The supervisor can then retry the specialist with feedback ("your output
    did not match the schema"). Max 2 retries before FAILED.
    """

    name = "output_schema_validator"

    def __init__(self, schema: Type[BaseModel]):
        self.schema = schema

    def check(self, output: Any, context: Optional[dict] = None) -> GuardrailResult:
        """Validate `output` against the schema.

        If `output` is a dict, try to parse it into the schema.
        If `output` is already a BaseModel instance, validate its type.
        """
        try:
            if isinstance(output, dict):
                self.schema(**output)
            elif isinstance(output, BaseModel):
                if not isinstance(output, self.schema):
                    return GuardrailResult(
                        status=GuardrailStatus.FAILED,
                        reason=f"Expected {self.schema.__name__}, got {type(output).__name__}",
                        guardrail_name=self.name,
                    )
            else:
                return GuardrailResult(
                    status=GuardrailStatus.FAILED,
                    reason=f"Output is {type(output).__name__}, expected dict or {self.schema.__name__}",
                    guardrail_name=self.name,
                )
            return GuardrailResult(
                status=GuardrailStatus.PASSED,
                reason="Schema validation passed",
                guardrail_name=self.name,
            )
        except ValidationError as e:
            return GuardrailResult(
                status=GuardrailStatus.FAILED,
                reason=f"Schema validation failed: {str(e)[:300]}",
                guardrail_name=self.name,
            )
        except Exception as e:
            return GuardrailResult(
                status=GuardrailStatus.ERROR,
                reason=f"Validator crashed: {type(e).__name__}: {str(e)[:200]}",
                guardrail_name=self.name,
            )


# ---------------------------------------------------------------------------
# 2. GroundingChecker
# ---------------------------------------------------------------------------


class GroundingChecker(OutputGuardrail):
    """
    Verifies that every material claim in a draft cites an evidence_id that
    exists in the run's evidence.

    ARCHITECTURE CORRECTION (Day 4 hardening):
        The original Day 4 version SILENTLY PASSED when no evidence_ids
        were in the context. This is wrong: if grounding is required (the
        draft has claims) but no evidence is available, that's a FAILURE.

        This version:
          1. Distinguishes "no claims to check" (PASSED) from "claims exist
             but no evidence to verify against" (FAILED).
          2. Distinguishes "claim has no evidence_id" (FAILED) from
             "claim cites nonexistent evidence_id" (FAILED with detail).
          3. Does NOT claim evidence-ID existence proves semantic truth.
             The check verifies the citation exists, not that the claim is
             semantically supported by the cited evidence. That deeper check
             requires an LLM judge (Day 7 eval).

    The context dict must contain 'evidence_ids' — a set/list of valid
    evidence IDs for the run.

    If a claim cites a non-existent evidence_id, the guardrail fails. The
    supervisor routes the run back to drafting with critic_feedback.
    """

    name = "grounding_checker"

    def check(self, output: Any, context: Optional[dict] = None) -> GuardrailResult:
        """Verify all claims in the draft are grounded in evidence."""
        try:
            context = context or {}
            evidence_ids = set(context.get("evidence_ids", []))
            require_grounding = context.get("require_grounding", True)

            # Extract claims from the output
            claims = self._extract_claims(output)

            # Case 1: No claims to verify → PASS
            if not claims:
                return GuardrailResult(
                    status=GuardrailStatus.PASSED,
                    reason="No claims to verify",
                    guardrail_name=self.name,
                )

            # Case 2: Claims exist but no evidence provided to verify against
            # This is a FAILURE (not a silent pass) when grounding is required.
            if not evidence_ids and require_grounding:
                return GuardrailResult(
                    status=GuardrailStatus.FAILED,
                    reason=f"{len(claims)} claims require grounding but no evidence_ids provided in context",
                    guardrail_name=self.name,
                )

            # If grounding is NOT required (e.g., test mode), skip
            if not require_grounding:
                return GuardrailResult(
                    status=GuardrailStatus.PASSED,
                    reason="Grounding check skipped (require_grounding=False)",
                    guardrail_name=self.name,
                )

            # Case 3: Check each claim
            missing_evidence_id = []  # claims with no evidence_id field
            ungrounded = []  # claims citing nonexistent evidence_id
            for claim in claims:
                eid = (
                    claim.get("evidence_id") if isinstance(claim, dict)
                    else getattr(claim, "evidence_id", None)
                )
                if not eid:
                    # Claim has no evidence_id at all
                    missing_evidence_id.append(claim)
                elif eid not in evidence_ids:
                    # Claim cites an evidence_id that doesn't exist
                    ungrounded.append(eid)

            if missing_evidence_id:
                return GuardrailResult(
                    status=GuardrailStatus.FAILED,
                    reason=f"{len(missing_evidence_id)} claims have no evidence_id reference",
                    guardrail_name=self.name,
                )

            if ungrounded:
                return GuardrailResult(
                    status=GuardrailStatus.FAILED,
                    reason=f"Ungrounded claims (evidence_id not found): {ungrounded[:5]}",
                    guardrail_name=self.name,
                )

            # All claims grounded
            return GuardrailResult(
                status=GuardrailStatus.PASSED,
                reason=f"All {len(claims)} claims grounded in evidence",
                guardrail_name=self.name,
            )
        except Exception as e:
            return GuardrailResult(
                status=GuardrailStatus.ERROR,
                reason=f"GroundingChecker crashed: {type(e).__name__}: {str(e)[:200]}",
                guardrail_name=self.name,
            )

    def _extract_claims(self, output: Any) -> list:
        """Extract claims from the output (handles dict, BaseModel)."""
        if isinstance(output, dict):
            return output.get("claims", [])
        if hasattr(output, "claims"):
            return output.claims
        return []


# ---------------------------------------------------------------------------
# 3. LengthCap
# ---------------------------------------------------------------------------


class LengthCap(OutputGuardrail):
    """
    Enforces a maximum word count on the draft body.

    The Engineering Contract §2 sets the default at 250 words. This guardrail
    catches drafts that exceed the cap; the supervisor routes back to drafting.
    """

    name = "length_cap"

    def __init__(self, max_words: int = 250):
        self.max_words = max_words

    def check(self, output: Any, context: Optional[dict] = None) -> GuardrailResult:
        """Check the body word count."""
        try:
            body = self._extract_body(output)
            if body is None:
                return GuardrailResult(
                    status=GuardrailStatus.PASSED,
                    reason="No body to check",
                    guardrail_name=self.name,
                )

            word_count = len(body.split())
            if word_count > self.max_words:
                return GuardrailResult(
                    status=GuardrailStatus.FAILED,
                    reason=f"Body exceeds length cap: {word_count} words > {self.max_words}",
                    guardrail_name=self.name,
                )

            return GuardrailResult(
                status=GuardrailStatus.PASSED,
                reason=f"Body length OK: {word_count} words <= {self.max_words}",
                guardrail_name=self.name,
            )
        except Exception as e:
            return GuardrailResult(
                status=GuardrailStatus.ERROR,
                reason=f"LengthCap crashed: {type(e).__name__}: {str(e)[:200]}",
                guardrail_name=self.name,
            )

    def _extract_body(self, output: Any) -> Optional[str]:
        if isinstance(output, dict):
            return output.get("body")
        if hasattr(output, "body"):
            return output.body
        return None


# ---------------------------------------------------------------------------
# 4. RecipientValidator
# ---------------------------------------------------------------------------


class RecipientValidator(OutputGuardrail):
    """
    Verifies that the draft recipient matches the lead's contact email.

    The context dict must contain 'expected_recipient' — the lead's email.

    This prevents a compromised LLM from swapping the recipient post-approval.
    """

    name = "recipient_validator"

    def check(self, output: Any, context: Optional[dict] = None) -> GuardrailResult:
        """Verify the draft recipient matches the expected recipient."""
        try:
            context = context or {}
            expected = context.get("expected_recipient")

            if expected is None:
                return GuardrailResult(
                    status=GuardrailStatus.PASSED,
                    reason="No expected_recipient in context; skipping",
                    guardrail_name=self.name,
                )

            actual = self._extract_recipient(output)
            if actual is None:
                return GuardrailResult(
                    status=GuardrailStatus.FAILED,
                    reason="Draft has no recipient field",
                    guardrail_name=self.name,
                )

            if actual.lower() != expected.lower():
                return GuardrailResult(
                    status=GuardrailStatus.FAILED,
                    reason=f"Recipient mismatch: expected {expected}, got {actual}",
                    guardrail_name=self.name,
                )

            return GuardrailResult(
                status=GuardrailStatus.PASSED,
                reason="Recipient matches expected",
                guardrail_name=self.name,
            )
        except Exception as e:
            return GuardrailResult(
                status=GuardrailStatus.ERROR,
                reason=f"RecipientValidator crashed: {type(e).__name__}: {str(e)[:200]}",
                guardrail_name=self.name,
            )

    def _extract_recipient(self, output: Any) -> Optional[str]:
        if isinstance(output, dict):
            return output.get("recipient")
        if hasattr(output, "recipient"):
            return output.recipient
        return None


__all__ = [
    "OutputSchemaValidator",
    "GroundingChecker",
    "LengthCap",
    "RecipientValidator",
]
