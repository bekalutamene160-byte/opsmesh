"""
OPSMESH Adapters — external provider interfaces + mock implementations.

Architecture (ADR-008: Mock-first adapter strategy):
  Every external dependency has TWO implementations:
    1. Real adapter (e.g., GroqProvider, HubSpotCRMProvider) — used in prod.
    2. Mock adapter (e.g., MockLLMProvider, MockCRMProvider) — used in:
         - CI (no API keys, no network, deterministic)
         - Local dev (no API keys needed)
         - The reproducible demo

  Both implement the same Protocol. The mock is the default; the real
  adapter is selected via env vars.

Day 2 scope: Protocols + mocks ONLY. Real adapters come in Day 6 (deploy)
or when we evaluate Groq vs Gemini (Correction 13 — deferred).

Layout:
    adapters/
      llm/        base.py (Protocol + LLMResponse), mock.py
      crm/        base.py (Protocol + CRMLead),     mock.py
      email/      base.py (Protocol),               mock.py
      research/   base.py (Protocol),               mock.py
      notifier/   base.py (Protocol),               mock.py
"""
