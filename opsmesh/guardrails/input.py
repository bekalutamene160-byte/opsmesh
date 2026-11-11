"""
OPSMESH Input Guardrails — run BEFORE the LLM sees the payload.

ARCHITECTURE CORRECTION (Day 4 hardening):
  1. PIIRedactor: improved recursive processing for nested dicts + lists.
     Documented limitations (not a complete PII detector).
  2. PromptInjectionHeuristic: preserves STRUCTURED findings (warnings list)
     so downstream components can inspect them. Explicit blocking vs
     non-blocking. Documented as NOT complete protection.

Both produce GuardrailResult (passed | failed | error). The 3-valued result
prevents the silent bypass bug from Course Case 8.
"""

from __future__ import annotations

import re
from typing import Any

from opsmesh.guardrails.base import GuardrailResult, GuardrailStatus, InputGuardrail


# ---------------------------------------------------------------------------
# 1. PIIRedactor
# ---------------------------------------------------------------------------


# PII patterns (simplified for Day 4; Day 6 adds more)
# NOTE: phone pattern matches 10-15 digits (excludes 16+ digit credit cards).
# Credit cards (13-19 digits) are matched by the credit_card pattern FIRST.
PII_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "phone": re.compile(r"\+?\d{10,15}(?!\d)"),
    "credit_card": re.compile(r"\b\d{13,19}\b"),
}


class PIIRedactor(InputGuardrail):
    """
    Masks PII in the payload BEFORE it reaches the LLM.

    Replaces:
      - emails        → u***@domain.com
      - phones        → +1-555-***-****
      - credit cards  → [REDACTED_CC]

    Recursively processes nested dicts AND lists (arbitrarily deep).

    LIMITATIONS (documented, not a complete PII detector):
      - Does NOT detect SSNs, passport numbers, addresses, names, or
        unstructured PII. Day 6 may add a named-entity recognizer.
      - The regex patterns are simplified — they may have false positives
        (e.g., a long ID number matches credit_card) or false negatives
        (e.g., a phone written as "five five five").
      - This is DEFENSE-IN-DEPTH, not a complete PII solution. The DB
        stores raw PII for the operator's use; the redaction only applies
        to what's sent to the LLM.
    """

    name = "pii_redactor"

    def check(self, payload: dict[str, Any]) -> GuardrailResult:
        """Redact PII in all string values of the payload (recursively)."""
        try:
            modified = self._redact_value(payload)
            # Check if anything actually changed
            if modified != payload:
                return GuardrailResult(
                    status=GuardrailStatus.PASSED,
                    reason="PII redacted",
                    modified_payload=modified,
                    guardrail_name=self.name,
                )
            return GuardrailResult(
                status=GuardrailStatus.PASSED,
                reason="No PII detected",
                modified_payload=None,
                guardrail_name=self.name,
            )
        except Exception as e:
            return GuardrailResult(
                status=GuardrailStatus.ERROR,
                reason=f"PIIRedactor failed: {type(e).__name__}: {str(e)[:200]}",
                guardrail_name=self.name,
            )

    def _redact_value(self, value: Any) -> Any:
        """Recursively redact PII in any value type."""
        if isinstance(value, str):
            return self._redact_string(value)
        elif isinstance(value, dict):
            return {k: self._redact_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [self._redact_value(v) for v in value]
        else:
            return value

    def _redact_string(self, s: str) -> str:
        """Redact PII in a single string.

        Order matters: credit_card must be checked BEFORE phone, because
        a 16-digit credit card number also matches the phone regex."""
        result = s
        # Credit card FIRST (13-19 digits) — full redaction
        result = PII_PATTERNS["credit_card"].sub("[REDACTED_CC]", result)
        # Email: mask the local part, keep domain
        result = PII_PATTERNS["email"].sub(
            lambda m: f"u***@{m.group().split('@')[1]}" if "@" in m.group() else "[REDACTED]",
            result,
        )
        # Phone LAST: mask middle digits
        result = PII_PATTERNS["phone"].sub(
            lambda m: m.group()[:4] + "***" + m.group()[-3:] if len(m.group()) >= 7 else "[REDACTED_PHONE]",
            result,
        )
        return result


# ---------------------------------------------------------------------------
# 2. PromptInjectionHeuristic
# ---------------------------------------------------------------------------


# Known injection patterns (case-insensitive substring match)
INJECTION_PATTERNS: list[str] = [
    "ignore previous",
    "ignore the above",
    "disregard the above",
    "disregard previous",
    "system prompt:",
    "you are actually",
    "you are now",
    "new instructions:",
    "override instructions",
    "forget your instructions",
    "act as if",
    "pretend you are",
    "ignore all previous",
    "do not follow",
    "secret mode",
    "developer mode",
    "jailbreak",
]


class InjectionFinding:
    """A single injection finding (structured, inspectable by downstream)."""

    def __init__(self, pattern: str, location: str):
        self.pattern = pattern  # the matched pattern
        self.location = location  # where in the payload it was found


class PromptInjectionHeuristic(InputGuardrail):
    """
    Flags known prompt-injection patterns in the payload.

    Detection: case-insensitive substring match against a list of known
    injection phrases. This is a HEURISTIC — it catches known patterns but
    CANNOT catch novel attacks.

    STRUCTURED FINDINGS (Day 4 hardening):
        The GuardrailResult's `reason` field contains a JSON-serialized list
        of findings, so downstream components (Critic agent, audit log) can
        inspect WHICH patterns were detected and WHERE.

        This is NOT complete prompt-injection protection. Novel attacks
        will bypass this. The system relies on:
          1. This heuristic (catches known patterns)
          2. ToolGateway arg allowlists (catches forged recipients)
          3. Gate 2 human approval (final defense)
          4. Audit log (forensic analysis)

    On detection:
      - block_on_detection=True (default): BLOCK the workflow (FAILED).
      - block_on_detection=False: flag but don't block (for retrieved web
        content where false positives would be too disruptive).

    LIMITATIONS (documented):
      - Only catches KNOWN patterns. Novel attacks bypass this.
      - Does NOT understand semantic meaning — a paraphrased injection
        ("please disregard everything before this") may not match.
      - Should NOT be the only defense. Use with ToolGateway + Gate 2 + audit.
    """

    name = "prompt_injection_heuristic"

    def __init__(self, block_on_detection: bool = True):
        self.block_on_detection = block_on_detection

    def check(self, payload: dict[str, Any]) -> GuardrailResult:
        """Scan all string values in the payload for injection patterns.

        Returns structured findings in the `reason` field so downstream
        components can inspect them.
        """
        try:
            findings: list[InjectionFinding] = []
            self._scan_value(payload, "", findings)

            if findings:
                # Serialize findings for the reason field
                findings_str = "; ".join(
                    f"'{f.pattern}' at {f.location}" for f in findings[:5]
                )
                if self.block_on_detection:
                    return GuardrailResult(
                        status=GuardrailStatus.FAILED,
                        reason=f"Injection detected: {findings_str}",
                        guardrail_name=self.name,
                    )
                else:
                    # Non-blocking: flag but pass. Downstream sees the warning.
                    return GuardrailResult(
                        status=GuardrailStatus.PASSED,
                        reason=f"WARNING — injection suspected (not blocked): {findings_str}",
                        guardrail_name=self.name,
                    )
            return GuardrailResult(
                status=GuardrailStatus.PASSED,
                reason="No injection patterns detected",
                guardrail_name=self.name,
            )
        except Exception as e:
            return GuardrailResult(
                status=GuardrailStatus.ERROR,
                reason=f"Heuristic failed: {type(e).__name__}: {str(e)[:200]}",
                guardrail_name=self.name,
            )

    def _scan_value(
        self,
        value: Any,
        path: str,
        findings: list[InjectionFinding],
    ) -> None:
        """Recursively scan for injection patterns. Appends to findings."""
        if isinstance(value, str):
            match = self._scan_string(value)
            if match:
                findings.append(InjectionFinding(pattern=match, location=path or "root"))
        elif isinstance(value, dict):
            for k, v in value.items():
                self._scan_value(v, f"{path}.{k}" if path else k, findings)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                self._scan_value(item, f"{path}[{i}]", findings)

    def _scan_string(self, s: str) -> str:
        """Check a single string for injection patterns. Returns the first match."""
        lower = s.lower()
        for pattern in INJECTION_PATTERNS:
            if pattern in lower:
                return pattern
        return ""


__all__ = [
    "PII_PATTERNS",
    "PIIRedactor",
    "INJECTION_PATTERNS",
    "InjectionFinding",
    "PromptInjectionHeuristic",
]
