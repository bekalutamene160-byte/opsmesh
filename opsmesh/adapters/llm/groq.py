"""
OPSMESH Groq LLM Provider — real LLM calls via Groq API.

Architecture (ADR-002):
  Implements the LLMProvider Protocol using httpx (no Groq SDK dependency).
  Groq's API is OpenAI-compatible: POST /openai/v1/chat/completions.

  MockLLMProvider remains the default for CI. This adapter is used when
  OPSMESH_LLM_PROVIDER=groq + OPSMESH_LLM_API_KEY is set.

MODEL SELECTION (Phase 7 — November 2026):
  Groq removed `llama-3.3-70b-versatile` from the free/developer tier in
  August 2026 (now Enterprise-only). The recommended free-tier replacement
  is GPT-OSS 120B (`openai/gpt-oss-120b`) — OpenAI's open-weight flagship.

  Default model: openai/gpt-oss-120b
  Free tier limits: 250K TPM, 1K RPM
  Pricing (per 1M tokens): $0.15 input, $0.60 output (cached input: $0.075)

Cost reporting:
  Groq returns usage.prompt_tokens + usage.completion_tokens in the
  response. Cost is computed from the model pricing table below and added
  to the run's budget_used_cost_usd.

Structured output:
  When response_schema is provided, the adapter requests JSON output and
  parses the response into the Pydantic model. If parsing fails,
  finish_reason='error'.

Timeout handling:
  Uses httpx with the specified timeout. On timeout, raises asyncio.TimeoutError
  (the supervisor catches this and transitions to FAILED or SEND_UNKNOWN).
"""

from __future__ import annotations

import json
from typing import Optional, Type

import httpx
from pydantic import BaseModel

from opsmesh.adapters.llm.base import LLMProvider, LLMResponse, Message
from opsmesh.models import utcnow


# Groq API endpoints
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Pricing per 1M tokens (verified Nov 2026 from console.groq.com/docs/models)
# Source: Groq pricing page; aipricing.guru; models.litellm.ai
MODEL_PRICING = {
    # Production models (free tier available)
    "openai/gpt-oss-120b":      {"input": 0.15, "output": 0.60},   # GPT-OSS 120B (flagship)
    "openai/gpt-oss-20b":      {"input": 0.075, "output": 0.30},  # GPT-OSS 20B (cheaper)
    # Enterprise-only (Contact Sales — kept for backward compatibility)
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
    "llama-3.1-8b-instant":    {"input": 0.05, "output": 0.08},
}


class GroqProvider:
    """
    Groq LLM provider. Implements LLMProvider Protocol.

    Uses httpx directly (no groq SDK) to minimize dependencies.
    Groq's API is OpenAI-compatible.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "openai/gpt-oss-120b",
    ):
        self.api_key = api_key
        self.default_model = model
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=GROQ_BASE_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._client

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
        """Call the Groq chat completions API."""
        actual_model = model or self.default_model
        client = await self._get_client()

        # Build the request payload
        payload: dict = {
            "model": actual_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Request JSON output if a schema is provided
        if response_schema is not None:
            payload["response_format"] = {"type": "json_object"}

        # Make the API call
        response = await client.post(
            "/chat/completions",
            json=payload,
            timeout=timeout,
        )

        if response.status_code == 429:
            # Rate limited
            retry_after = response.headers.get("retry-after")
            from opsmesh.retry import RateLimitError
            raise RateLimitError(
                "Groq rate limit exceeded",
                retry_after=float(retry_after) if retry_after else None,
            )

        if response.status_code >= 500:
            from opsmesh.retry import TransientError
            raise TransientError(
                f"Groq server error: {response.status_code} {response.text[:200]}"
            )

        if response.status_code >= 400:
            raise ValueError(
                f"Groq API error: {response.status_code} {response.text[:200]}"
            )

        data = response.json()

        # Extract the response
        choice = data["choices"][0]
        content = choice["message"]["content"]
        finish_reason_raw = choice.get("finish_reason", "stop")

        # Map finish reasons
        finish_reason_map = {
            "stop": "stop",
            "length": "length",
            "tool_calls": "tool_call",
        }
        finish_reason = finish_reason_map.get(finish_reason_raw, "error")

        # Extract token usage
        usage = data.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

        # Calculate cost
        pricing = MODEL_PRICING.get(actual_model, {"input": 0.10, "output": 0.40})
        cost_usd = (
            input_tokens * pricing["input"] + output_tokens * pricing["output"]
        ) / 1_000_000

        # Parse structured output if requested
        structured = None
        if response_schema is not None and finish_reason == "stop":
            try:
                structured = response_schema.model_validate_json(content)
            except Exception:
                finish_reason = "error"

        return LLMResponse(
            content=content,
            structured=structured,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            model=actual_model,
            finish_reason=finish_reason,
        )

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


__all__ = ["GroqProvider"]
