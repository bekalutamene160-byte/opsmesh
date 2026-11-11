"""
OPSMESH Exa Research Provider — real web search via Exa API.

Architecture:
  Implements the ResearchProvider Protocol using httpx.
  Exa's API: POST https://api.exa.ai/search

  MockResearchProvider remains the default for CI. This adapter is used when
  OPSMESH_RESEARCH_PROVIDER=exa + OPSMESH_EXA_API_KEY is set.

Returns normalized WebSearchHit objects with URL, title, snippet, timestamp.
"""

from __future__ import annotations

from typing import Optional

import httpx

from opsmesh.adapters.research.base import ResearchProvider
from opsmesh.models import utcnow
from opsmesh.tools.schemas import WebSearchHit


EXA_BASE_URL = "https://api.exa.ai"


class ExaResearchProvider:
    """
    Exa research provider. Implements ResearchProvider Protocol.

    Uses httpx to call Exa's search API.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=EXA_BASE_URL,
                headers={
                    "x-api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(15.0, connect=10.0),
            )
        return self._client

    async def web_search(
        self, query: str, max_results: int = 5
    ) -> list[WebSearchHit]:
        """Search the web via Exa. Returns normalized WebSearchHit list."""
        client = await self._get_client()

        payload = {
            "query": query,
            "numResults": max_results,
            "contents": {
                "text": True,  # Include text content in results
            },
        }

        response = await client.post("/search", json=payload)

        if response.status_code == 429:
            from opsmesh.retry import RateLimitError
            raise RateLimitError("Exa rate limit exceeded")

        if response.status_code >= 500:
            from opsmesh.retry import TransientError
            raise TransientError(
                f"Exa server error: {response.status_code}"
            )

        if response.status_code >= 400:
            raise ValueError(
                f"Exa API error: {response.status_code} {response.text[:200]}"
            )

        data = response.json()
        results = data.get("results", [])

        hits = []
        for r in results:
            url = r.get("url", "")
            title = r.get("title", "")
            # Exa returns text in "text" field when contents.text=True
            snippet = r.get("text", "")[:500]  # truncate to snippet length

            if url and title:
                hits.append(WebSearchHit(
                    url=url,
                    title=title,
                    snippet=snippet or "",
                    retrieved_at=utcnow(),
                ))

        return hits[:max_results]

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


__all__ = ["ExaResearchProvider"]
