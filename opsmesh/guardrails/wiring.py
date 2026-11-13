"""
OPSMESH Guardrail Wiring — connects the guardrails/ package to the runtime.

Before this module, the guardrail classes (PIIRedactor, PromptInjectionHeuristic,
LengthCap, RecipientValidator, OutputSchemaValidator) were implemented and
unit-tested in isolation but NEVER imported by any runtime module — they were
dormant. This module exposes pre-built chains + helpers that the specialists
call so the guardrails actually run on every lead.

Input chain  (runs BEFORE the LLM sees lead fields):
    PIIRedactor           — masks emails/phones/credit-cards in the prompt
    PromptInjectionHeuristic — blocks "ignore previous instructions" etc.

Output chain (runs BEFORE a DraftResult becomes an approval payload):
    OutputSchemaValidator — LLM output matches DraftResult schema
    LengthCap(250)         — draft body under the word cap
    RecipientValidator    — draft recipient == lead contact email
"""
from __future__ import annotations

from typing import Any, Optional

from opsmesh.guardrails.base import GuardrailChain, GuardrailError, OutputGuardrailChain
from opsmesh.guardrails.input import PIIRedactor, PromptInjectionHeuristic
from opsmesh.guardrails.output import (
    LengthCap,
    OutputSchemaValidator,
    RecipientValidator,
)
from opsmesh.models import DraftResult


# Module-level singletons (guardrails are stateless, safe to share).
_INPUT_CHAIN: Optional[GuardrailChain] = None
_OUTPUT_CHAIN: Optional[OutputGuardrailChain] = None


def get_input_chain() -> GuardrailChain:
    """Lazily build the input guardrail chain (PII + injection)."""
    global _INPUT_CHAIN
    if _INPUT_CHAIN is None:
        _INPUT_CHAIN = GuardrailChain([
            PIIRedactor(),
            PromptInjectionHeuristic(block_on_detection=True),
        ])
    return _INPUT_CHAIN


def get_output_chain() -> OutputGuardrailChain:
    """Lazily build the output guardrail chain for DraftResult."""
    global _OUTPUT_CHAIN
    if _OUTPUT_CHAIN is None:
        _OUTPUT_CHAIN = OutputGuardrailChain([
            OutputSchemaValidator(schema=DraftResult),
            LengthCap(max_words=250),
            RecipientValidator(),
        ])
    return _OUTPUT_CHAIN


def apply_input_guardrails(payload: dict[str, Any]) -> tuple[dict[str, Any], Optional[str]]:
    """Run the input chain on a lead-fields payload.

    Returns (safe_payload, error_message).
      - If all guardrails pass: safe_payload is the (possibly PII-redacted)
        payload, error_message is None.
      - If any guardrail fails (e.g. injection detected): safe_payload is the
        original payload, error_message describes the failure so the caller
        can route the run to human review instead of proceeding.
    """
    try:
        safe = get_input_chain().run_input(payload)
        return safe, None
    except GuardrailError as e:
        # Injection detected (or redactor error) — fail closed.
        return payload, f"Input guardrail '{e.guardrail_name}' blocked: {e.result.reason}"


def apply_output_guardrails(
    draft: DraftResult, expected_recipient: Optional[str] = None
) -> tuple[DraftResult, Optional[str]]:
    """Run the output chain on a DraftResult.

    Returns (draft, error_message). If all pass, error_message is None.
    On failure, error_message explains which guardrail failed so the
    supervisor can route back to drafting with critic_feedback.
    """
    context = {"expected_recipient": expected_recipient} if expected_recipient else {}
    try:
        get_output_chain().run(draft, context=context)
        return draft, None
    except GuardrailError as e:
        return draft, f"Output guardrail '{e.guardrail_name}' failed: {e.result.reason}"


__all__ = [
    "get_input_chain",
    "get_output_chain",
    "apply_input_guardrails",
    "apply_output_guardrails",
]
