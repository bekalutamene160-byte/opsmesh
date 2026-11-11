"""
Research Provider Protocol — web search capability.

Architecture (Correction 9): web_search is an INLINE research capability,
NOT an MCP tool. It lives inside the Research specialist because:
  - It is read-only.
  - It has no credentials to protect (uses a free search provider).
  - Exposing it via MCP would add a network hop for no security benefit.

SSRF protection (Day 4 ToolGateway):
  - Outbound HTTP restricted to a configured domain allowlist.
  - Private IP ranges (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16,
    127.0.0.0/8, 169.254.0.0/16) blocked at the socket level.
  - Redirect following: disabled by default.

Day 2 scope: Protocol + Mock implementation. Real adapter (SerperProvider)
arrives when we evaluate providers (deferred per Correction 13).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from opsmesh.tools.schemas import WebSearchHit


@runtime_checkable
class ResearchProvider(Protocol):
    """The research provider contract."""

    async def web_search(
        self, query: str, max_results: int = 5
    ) -> list[WebSearchHit]:
        """Search the public web. Returns up to max_results hits.

        Raises:
            asyncio.TimeoutError: if the search exceeds the timeout.
            SSRFBlockedError: if the query attempts to reach a blocked
                domain or private IP (Day 4).
        """
        ...


__all__ = ["ResearchProvider"]
