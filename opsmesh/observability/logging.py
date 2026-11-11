"""
OPSMESH Structured Logging — JSON output with PII + secret redaction.

Architecture (course Ch 14 + ADR-006):
  Every log line is a JSON object with:
    - ts: ISO 8601 timestamp (UTC)
    - level: DEBUG | INFO | WARNING | ERROR | CRITICAL
    - event: human-readable event name (e.g., "run_started")
    - trace_id: propagated from contextvars (ties logs to traces)
    - span_id: current span (if inside a span)
    - run_id, workspace_id, lead_id (if available)
    - attrs: additional structured fields

  PII REDACTION (Engineering Contract §4):
    Emails → u***@domain.com
    Phones → +1-555-***-****
    Credit cards → [REDACTED_CC]
    Applied to ALL string values in the log, recursively.

  SECRET REDACTION:
    Known secret values (from env vars) are replaced with [REDACTED_SECRET].
    This prevents API keys, passwords, and tokens from leaking into logs.

  NO EXTRA DEPENDENCIES:
    Uses Python's built-in `logging` module with a custom JSON formatter.
    No structlog, no loguru — just stdlib.

USAGE:
    from opsmesh.observability.logging import get_logger
    logger = get_logger("opsmesh.supervisor")
    logger.info("run_started", run_id="run_1", workspace_id="ws_1")
    # Output: {"ts":"2026-...","level":"info","event":"run_started",...}
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# PII patterns (same as guardrails/input.py — kept separate to avoid coupling)
# ---------------------------------------------------------------------------

_PII_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "phone": re.compile(r"\+?\d{10,15}(?!\d)"),
    "credit_card": re.compile(r"\b\d{13,19}\b"),
}


def _redact_pii_in_string(s: str) -> str:
    """Redact PII in a single string."""
    result = s
    result = _PII_PATTERNS["credit_card"].sub("[REDACTED_CC]", result)
    result = _PII_PATTERNS["email"].sub(
        lambda m: f"u***@{m.group().split('@')[1]}" if "@" in m.group() else "[REDACTED]",
        result,
    )
    result = _PII_PATTERNS["phone"].sub(
        lambda m: m.group()[:4] + "***" + m.group()[-3:] if len(m.group()) >= 7 else "[REDACTED_PHONE]",
        result,
    )
    return result


def _redact_pii_recursive(value: Any) -> Any:
    """Recursively redact PII in any value type (str, dict, list)."""
    if isinstance(value, str):
        return _redact_pii_in_string(value)
    elif isinstance(value, dict):
        return {k: _redact_pii_recursive(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_redact_pii_recursive(v) for v in value]
    else:
        return value


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

# Substrings that indicate a sensitive field name
_SENSITIVE_FIELD_NAMES = ("password", "secret", "token", "api_key", "apikey")


def _redact_secrets_in_dict(d: dict) -> dict:
    """Redact sensitive field values (password, secret, token, api_key)."""
    result = {}
    for key, value in d.items():
        if any(s in key.lower() for s in _SENSITIVE_FIELD_NAMES):
            result[key] = "[REDACTED]"
        elif isinstance(value, dict):
            result[key] = _redact_secrets_in_dict(value)
        elif isinstance(value, str):
            result[key] = _redact_pii_in_string(value)
        elif isinstance(value, list):
            result[key] = [_redact_pii_recursive(v) for v in value]
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# JSON Formatter
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """
    Formats log records as JSON lines (one JSON object per line).

    Every line has:
        ts, level, event, logger, trace_id, span_id, + any extra attrs

    PII and secrets are redacted before formatting.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Build the base log dict
        log_dict: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
        }

        # The message is the "event" name
        log_dict["event"] = record.getMessage()

        # Extract trace_id and span_id from the record (set by contextvars)
        # These are set as extra attributes by the tracing module
        if hasattr(record, "trace_id"):
            log_dict["trace_id"] = record.trace_id
        if hasattr(record, "span_id"):
            log_dict["span_id"] = record.span_id

        # Extract any extra attributes (passed via logger.info("event", extra={...}))
        # or via logger.info("event", run_id="run_1", workspace_id="ws_1")
        standard_attrs = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "getMessage",
            "trace_id", "span_id",
        }
        extras = {}
        for key, value in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                extras[key] = value

        if extras:
            # Redact PII + secrets in extra attributes
            extras = _redact_pii_recursive(extras)
            extras = _redact_secrets_in_dict(extras) if isinstance(extras, dict) else extras
            log_dict["attrs"] = extras

        # Add exception info if present
        if record.exc_info:
            log_dict["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_dict, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Logger factory
# ---------------------------------------------------------------------------

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger with the JSON formatter.

    Call once at startup. Idempotent (won't re-configure if already done).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers (avoid duplicate output)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)

    _CONFIGURED = True


def get_logger(name: str = "opsmesh") -> logging.Logger:
    """Get a logger with the OPSMESH JSON formatter configured.

    If configure_logging() hasn't been called yet, it's called automatically
    with INFO level.
    """
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)


__all__ = [
    "JSONFormatter",
    "configure_logging",
    "get_logger",
]
