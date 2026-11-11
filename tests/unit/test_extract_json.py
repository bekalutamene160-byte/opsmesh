"""
Tests for the JSON extraction helper used by all specialists.

Verifies that _extract_json correctly handles:
  1. Plain JSON
  2. JSON wrapped in markdown code fences (```json ... ```)
  3. JSON with extra text before/after
  4. JSON with nested braces
  5. Invalid JSON (returns None)
  6. Empty content (returns None)
"""

from __future__ import annotations

import pytest

from opsmesh.specialists import _extract_json


class TestExtractJson:
    def test_plain_json(self):
        """Plain JSON object is parsed correctly."""
        content = '{"summary": "Test", "sufficient_for_qualification": true}'
        result = _extract_json(content)
        assert result is not None
        assert result["summary"] == "Test"
        assert result["sufficient_for_qualification"] is True

    def test_json_in_markdown_code_fence(self):
        """JSON wrapped in ```json ... ``` is parsed correctly."""
        content = '```json\n{"summary": "Test", "sufficient": true}\n```'
        result = _extract_json(content)
        assert result is not None
        assert result["summary"] == "Test"
        assert result["sufficient"] is True

    def test_json_in_plain_code_fence(self):
        """JSON wrapped in ``` ... ``` (no json label) is parsed correctly."""
        content = '```\n{"verdict": "APPROVED"}\n```'
        result = _extract_json(content)
        assert result is not None
        assert result["verdict"] == "APPROVED"

    def test_json_with_extra_text_before(self):
        """JSON with text before it is parsed correctly."""
        content = 'Here is the research:\n{"summary": "Test", "sufficient": true}'
        result = _extract_json(content)
        assert result is not None
        assert result["summary"] == "Test"

    def test_json_with_extra_text_after(self):
        """JSON with text after it is parsed correctly."""
        content = '{"summary": "Test"}\nHope this helps!'
        result = _extract_json(content)
        assert result is not None
        assert result["summary"] == "Test"

    def test_json_with_extra_text_before_and_after(self):
        """JSON with text before and after is parsed correctly."""
        content = 'Here is the result:\n{"verdict": "APPROVED"}\nLet me know if you need anything else.'
        result = _extract_json(content)
        assert result is not None
        assert result["verdict"] == "APPROVED"

    def test_json_with_nested_braces(self):
        """JSON with nested braces (e.g., arrays of objects) is parsed correctly."""
        content = '{"verdict": "REJECTED", "issues": [{"kind": "tone_violation", "detail": "Too salesy"}]}'
        result = _extract_json(content)
        assert result is not None
        assert result["verdict"] == "REJECTED"
        assert len(result["issues"]) == 1
        assert result["issues"][0]["kind"] == "tone_violation"

    def test_json_with_escaped_quotes(self):
        """JSON with escaped quotes in strings is parsed correctly."""
        content = '{"feedback": "The subject is \\"too long\\" for the limit"}'
        result = _extract_json(content)
        assert result is not None
        assert result["feedback"] == 'The subject is "too long" for the limit'

    def test_invalid_json_returns_none(self):
        """Invalid JSON returns None."""
        content = 'This is not JSON at all'
        result = _extract_json(content)
        assert result is None

    def test_empty_content_returns_none(self):
        """Empty string returns None."""
        result = _extract_json("")
        assert result is None

    def test_none_content_returns_none(self):
        """None returns None."""
        result = _extract_json(None)
        assert result is None

    def test_json_with_brace_in_string_value(self):
        """JSON with a brace character inside a string value is parsed correctly."""
        content = '{"body": "Use {placeholder} here", "subject": "Test"}'
        result = _extract_json(content)
        assert result is not None
        assert result["body"] == "Use {placeholder} here"
        assert result["subject"] == "Test"

    def test_json_in_markdown_with_extra_text(self):
        """JSON in markdown code fence with extra text is parsed correctly."""
        content = 'Here is the analysis:\n```json\n{"score": 0.85, "status": "QUALIFIED"}\n```\nGood luck!'
        result = _extract_json(content)
        assert result is not None
        assert result["score"] == 0.85
        assert result["status"] == "QUALIFIED"
