"""
MockLLMProvider — deterministic LLM for tests and local dev.

Architecture (ADR-008): mock-first. This provider NEVER makes a network call.
It returns canned responses keyed by the messages hash, so the same prompt
always produces the same response. This is what makes CI hermetic and the
demo reproducible.

How it works:
  1. Compute a stable hash of the messages list.
  2. Look up the hash in the fixtures dict.
  3. If found, return the canned response (parsed into response_schema if given).
  4. If not found, raise NoFixtureFound — tests fail loudly rather than
     silently passing with a wrong canned response.

The fixtures are populated:
  - In tests: via add_fixture() or load_fixtures_from_jsonl().
  - In production demos: via the eval scenario JSONL.

Day 2 default behavior: returns a simple canned response without fixtures
(useful for quick smoke tests). For deterministic eval, populate fixtures.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional, Type

from pydantic import BaseModel

from opsmesh.adapters.llm.base import LLMProvider, LLMResponse, Message
from opsmesh.models import utcnow


class NoFixtureFound(Exception):
    """Raised when the mock LLM has no canned response for the prompt hash.
    Tests should fail loudly rather than silently passing with a wrong
    canned response."""

    def __init__(self, prompt_hash: str, messages_preview: str):
        self.prompt_hash = prompt_hash
        super().__init__(
            f"No mock LLM fixture found for prompt_hash={prompt_hash}. "
            f"Add a fixture for this prompt. Messages preview: {messages_preview[:200]}"
        )


class MockLLMProvider:
    """
    Deterministic mock LLM provider.

    Usage:
        provider = MockLLMProvider()
        # Option A: provide a default canned response (smoke tests)
        provider.set_default_response("This is a mock response.")

        # Option B: register fixtures keyed by prompt hash (eval)
        provider.add_fixture(
            messages=[Message(role="user", content="Hello")],
            response=LLMResponse(content="Hi there", input_tokens=1,
                                  output_tokens=2, cost_usd=0.0, model="mock"),
        )

        # Use it
        response = await provider.chat(messages)
    """

    def __init__(self, default_response: Optional[str] = None) -> None:
        # _fixtures maps prompt_hash -> LLMResponse dict (JSON-serializable)
        self._fixtures: dict[str, dict] = {}
        self._default_response = default_response
        self._call_count = 0
        self._calls: list[list[Message]] = []  # spy for tests

    @staticmethod
    def _hash_messages(messages: list[Message]) -> str:
        """Stable hash of the messages list. Same prompt → same hash."""
        canonical = json.dumps(
            [{"role": m.role, "content": m.content} for m in messages],
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def add_fixture(
        self,
        messages: list[Message],
        response: LLMResponse,
    ) -> None:
        """Register a canned response for a specific prompt."""
        h = self._hash_messages(messages)
        self._fixtures[h] = response.model_dump(mode="json")

    def load_fixtures_from_jsonl(self, path: str) -> int:
        """Load fixtures from a JSONL file. Each line is a JSON object with
        'messages' and 'response' keys. Returns the count loaded."""
        count = 0
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                messages = [Message(**m) for m in data["messages"]]
                response = LLMResponse(**data["response"])
                self.add_fixture(messages, response)
                count += 1
        return count

    def set_default_response(self, content: str) -> None:
        """Set a default response for prompts without a registered fixture.
        Useful for smoke tests. For eval, use add_fixture instead."""
        self._default_response = content

    @property
    def call_count(self) -> int:
        """How many times chat() was called. For test assertions."""
        return self._call_count

    async def chat(
        self,
        messages: list[Message],
        *,
        model: Optional[str] = None,
        response_schema: Optional[Type[BaseModel]] = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: float = 30.0,
    ) -> LLMResponse:
        """Return the canned response for the prompt, or raise NoFixtureFound."""
        self._call_count += 1
        self._calls.append(messages)

        h = self._hash_messages(messages)

        if h in self._fixtures:
            data = self._fixtures[h]
            response = LLMResponse(**data)
        elif self._default_response is not None:
            # Use the default canned response
            response = LLMResponse(
                content=self._default_response,
                input_tokens=10,
                output_tokens=len(self._default_response.split()),
                cost_usd=0.0,
                model=model or "mock-llm",
                finish_reason="stop",
            )
        else:
            preview = json.dumps(
                [{"role": m.role, "content": m.content[:50]} for m in messages]
            )
            raise NoFixtureFound(h, preview)

        # If a schema was requested, try to parse the content as JSON
        if response_schema is not None and response.structured is None:
            try:
                parsed = response_schema.model_validate_json(response.content)
                response.structured = parsed
            except Exception:
                # Parse failure — caller (output guardrail Day 4) handles
                response.finish_reason = "error"

        return response


__all__ = ["MockLLMProvider", "NoFixtureFound"]
