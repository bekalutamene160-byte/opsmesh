"""
LLM Provider Protocol — the interface every LLM adapter must implement.

Architecture (ADR-002): LLM provider abstraction with deterministic mock.
  - The supervisor and specialists reference ONLY this Protocol.
  - Real adapters (GroqProvider, GeminiProvider) implement the same contract.
  - MockLLMProvider returns canned responses from fixtures (deterministic).
  - Switching providers is a config change, not a code change.

The chat() method:
  - Accepts a list of Message (system + user turns).
  - Optionally accepts response_schema (a Pydantic BaseModel subclass) for
    structured outputs. If provided, the LLM must return JSON matching that
    schema; the adapter parses it into the schema instance.
  - Returns an LLMResponse with content, structured output, token counts,
    cost, model name, and finish_reason.

Cost reporting is MANDATORY — without per-call cost, the RunBudget (Day 3)
cannot enforce the per-run cost cap, and the dashboard cannot show spend.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Protocol, Type, runtime_checkable

from pydantic import BaseModel, Field

from opsmesh.models import utcnow


class Message(BaseModel):
    """A single chat message. role is one of system/user/assistant."""
    role: Literal["system", "user", "assistant"]
    content: str


class LLMResponse(BaseModel):
    """
    The response from an LLM call.

    Fields:
        content       — the raw text response.
        structured    — if response_schema was provided, the parsed Pydantic
                        instance. None if no schema was requested OR if parsing
                        failed (finish_reason='error').
        input_tokens  — tokens in the prompt (for budget + cost).
        output_tokens — tokens in the response.
        cost_usd      — cost of this call in USD (provider's pricing).
        model         — the actual model name used (e.g., 'llama-3.3-70b').
        finish_reason — why the model stopped:
                          'stop'      — natural end (success)
                          'length'    — hit max_tokens (truncated)
                          'tool_call' — model wants to call a tool (Day 4)
                          'error'     — provider error or parse failure
    """

    content: str
    structured: Optional[BaseModel] = None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    model: str
    finish_reason: Literal["stop", "length", "tool_call", "error"] = "stop"
    completed_at: Any = Field(default_factory=utcnow)


@runtime_checkable
class LLMProvider(Protocol):
    """
    The LLM provider contract.

    All adapters (Mock, Groq, Gemini) implement this. The supervisor and
    specialists reference this Protocol — they never import a concrete class.
    """

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
        """
        Send messages to the LLM and return the response.

        Args:
            messages: list of Message (system + user turns).
            model: optional model override; if None, uses provider default.
            response_schema: optional Pydantic model class. If provided, the
                adapter requests structured output and parses the response
                into this schema. The supervisor's OutputSchemaValidator
                guardrail (Day 4) re-validates regardless.
            max_tokens: hard cap on output tokens.
            temperature: 0.0 = deterministic (recommended for agents).
            timeout: hard timeout in seconds. The Engineering Contract
                (§1) caps this at 30s.

        Returns:
            LLMResponse with content, structured (if schema), tokens, cost.

        Raises:
            asyncio.TimeoutError: if the call exceeds `timeout`.
            ProviderError: on 5xx from the provider (Day 3 retry handles).
            RateLimitError: on 429 (Day 3 retry honors Retry-After).
        """
        ...


__all__ = ["Message", "LLMResponse", "LLMProvider"]
