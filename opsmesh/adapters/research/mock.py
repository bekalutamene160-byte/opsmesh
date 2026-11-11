"""
MockResearchProvider — returns canned search results.

Architecture (ADR-008): mock-first. Returns canned WebSearchHit objects
keyed by query substring match. Never makes a network call.
"""

from __future__ import annotations

from pydantic import HttpUrl

from opsmesh.adapters.research.base import ResearchProvider
from opsmesh.models import utcnow
from opsmesh.tools.schemas import WebSearchHit


class MockResearchProvider:
    """
    Mock research provider. Returns canned results.

    Usage:
        research = MockResearchProvider()
        research.add_result(
            query_substring="acme",
            hits=[WebSearchHit(url="https://acme.com", title="Acme", snippet="...")],
        )
        results = await research.web_search("acme corp")
    """

    def __init__(self) -> None:
        # _results maps query_substring -> list of WebSearchHit
        self._results: dict[str, list[WebSearchHit]] = {}
        self._call_count = 0

    def add_result(
        self, query_substring: str, hits: list[WebSearchHit]
    ) -> None:
        """Register canned results for queries containing the substring."""
        self._results[query_substring.lower()] = hits

    @property
    def call_count(self) -> int:
        return self._call_count

    async def web_search(
        self, query: str, max_results: int = 5
    ) -> list[WebSearchHit]:
        self._call_count += 1

        # Find a registered result whose substring matches the query
        query_lower = query.lower()
        for substring, hits in self._results.items():
            if substring in query_lower:
                return hits[:max_results]

        # Default: return empty results (no match found)
        # Tests that need specific results should register them via add_result()
        return []


__all__ = ["MockResearchProvider"]
