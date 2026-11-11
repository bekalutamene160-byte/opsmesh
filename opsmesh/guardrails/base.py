"""
OPSMESH Guardrail base — the 3-valued result type.

Architecture (course Ch 13 + Correction for Case 8):
  Guardrails are pre/post hooks that validate inputs, outputs, and tool args.
  They are the defense-in-depth layer that catches what the LLM misses.

THE 3-VALUED RESULT (Correction for Course Case 8):
  The original course used a boolean: True = passed, False = failed.
  The bug: on a parse error, the guardrail returned False, and the calling
  code treated False as "no issues found" — silently bypassing the check.

  The fix: use THREE values:
    - passed  : guardrail ran and found no issues
    - failed  : guardrail ran and found issues
    - error   : guardrail failed to run (exception, parse error, etc.)

  `error` is treated as `failed` (fail-closed). This prevents the silent
  bypass that caused Case 8.

Guardrail classes (course Ch 13):
  Three classes:
    1. Input guardrails  — run BEFORE the LLM sees the payload
    2. Output guardrails — run BEFORE the LLM output becomes an action
    3. Tool guardrails   — the ToolGateway itself (Day 4)

  Each guardrail has a check() method that returns a GuardrailResult.
  The GuardrailChain runs all guardrails in sequence; the first failure
  (or error) stops the chain.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class GuardrailStatus(str, Enum):
    """The 3-valued result. `error` is treated as `failed` (fail-closed)."""
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"  # guardrail itself failed to run


class GuardrailResult(BaseModel):
    """
    The result of running a guardrail.

    Fields:
        status:      passed | failed | error
        reason:      human-readable explanation (for audit log / dashboard)
        modified_payload: if the guardrail modified the payload (e.g., PII
                         redaction), the modified version goes here. None
                         means the payload is unchanged.
        guardrail_name: the name of the guardrail that produced this result.
    """

    status: GuardrailStatus
    reason: str = ""
    modified_payload: Optional[dict[str, Any]] = None
    guardrail_name: str = ""

    @property
    def is_safe(self) -> bool:
        """True if the guardrail passed. `error` is NOT safe (fail-closed)."""
        return self.status == GuardrailStatus.PASSED


class GuardrailError(Exception):
    """Raised when a GuardrailChain fails (first failure stops the chain)."""

    def __init__(self, guardrail_name: str, result: GuardrailResult):
        self.guardrail_name = guardrail_name
        self.result = result
        super().__init__(
            f"Guardrail '{guardrail_name}' {result.status.value}: {result.reason}"
        )


class InputGuardrail:
    """
    Base class for input guardrails. Subclass and implement check().

    Input guardrails run BEFORE the LLM sees the payload. They can:
      - Block the payload (return FAILED)
      - Modify the payload (return PASSED with modified_payload, e.g., PII redaction)
      - Pass it through unchanged (return PASSED with modified_payload=None)
    """

    name: str = "unnamed_guardrail"

    def check(self, payload: dict[str, Any]) -> GuardrailResult:
        """Override in subclass. Return a GuardrailResult."""
        raise NotImplementedError(f"{self.__class__.__name__} must implement check()")


class OutputGuardrail:
    """
    Base class for output guardrails. Subclass and implement check().

    Output guardrails run BEFORE the LLM output becomes an action. They
    validate that the output matches the expected schema, is grounded in
    evidence, meets length limits, etc.
    """

    name: str = "unnamed_output_guardrail"

    def check(self, output: Any, context: Optional[dict] = None) -> GuardrailResult:
        """Override in subclass. `context` may contain evidence, run record, etc."""
        raise NotImplementedError(f"{self.__class__.__name__} must implement check()")


class GuardrailChain:
    """
    Runs a sequence of guardrails. The first failure (or error) stops the chain.

    Usage:
        chain = GuardrailChain([PIIRedactor(), PromptInjectionHeuristic()])
        safe_payload = chain.run_input(payload)

    If any guardrail fails, raises GuardrailError. If all pass, returns the
    (possibly modified) payload.
    """

    def __init__(self, guardrails: list[InputGuardrail]):
        self.guardrails = guardrails

    def run_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run all input guardrails in sequence.

        Returns the (possibly modified) payload if all pass.
        Raises GuardrailError if any guardrail fails or errors.
        """
        current_payload = payload
        for guardrail in self.guardrails:
            try:
                result = guardrail.check(current_payload)
            except Exception as e:
                # The guardrail itself crashed — treat as error (fail-closed)
                result = GuardrailResult(
                    status=GuardrailStatus.ERROR,
                    reason=f"Guardrail raised {type(e).__name__}: {str(e)[:200]}",
                    guardrail_name=guardrail.name,
                )

            if not result.is_safe:
                raise GuardrailError(guardrail.name, result)

            # Apply modifications if the guardrail changed the payload
            if result.modified_payload is not None:
                current_payload = result.modified_payload

        return current_payload


class OutputGuardrailChain:
    """
    Runs a sequence of output guardrails. The first failure stops the chain.

    Usage:
        chain = OutputGuardrailChain([
            OutputSchemaValidator(schema=DraftResult),
            GroundingChecker(),
            LengthCap(max_words=250),
            RecipientValidator(),
        ])
        chain.run(draft_output, context={"evidence": evidence_items})
    """

    def __init__(self, guardrails: list[OutputGuardrail]):
        self.guardrails = guardrails

    def run(self, output: Any, context: Optional[dict] = None) -> Any:
        """Run all output guardrails in sequence.

        Returns the output if all pass.
        Raises GuardrailError if any guardrail fails or errors.
        """
        current_output = output
        for guardrail in self.guardrails:
            try:
                result = guardrail.check(current_output, context)
            except Exception as e:
                result = GuardrailResult(
                    status=GuardrailStatus.ERROR,
                    reason=f"Guardrail raised {type(e).__name__}: {str(e)[:200]}",
                    guardrail_name=guardrail.name,
                )

            if not result.is_safe:
                raise GuardrailError(guardrail.name, result)

        return current_output


__all__ = [
    "GuardrailStatus",
    "GuardrailResult",
    "GuardrailError",
    "InputGuardrail",
    "OutputGuardrail",
    "GuardrailChain",
    "OutputGuardrailChain",
]
