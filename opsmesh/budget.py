"""
OPSMESH RunBudget — per-run caps on tokens, cost, latency, and tool calls.

Architecture (course Ch 15):
  Without budgets, a single run can burn a dollar and take 30 seconds. At
  scale, that's a budget-burning, latency-spike machine. Budgets are the
  engineering discipline that says: "this run is allowed to cost up to
  $0.05 and take up to 60 seconds; if either is exceeded, degrade or stop."

ENGINEERING CONTRACT DEFAULTS (§2):
  max_tokens:       8,000
  max_cost_usd:     $0.05
  max_seconds:      60.0
  max_tool_calls:   12

BUDGET EXHAUSTION MUST BE VISIBLE (course Tip #15):
  When a budget is exceeded, the right behavior is:
    1. Persist current state (checkpoint).
    2. Throw BudgetExceeded.
    3. The supervisor catches it and transitions the run to FAILED.
  The WRONG behavior is to silently let the run continue — that's how
  free-tier API quotas disappear overnight.

MODEL ROUTING (course Ch 15):
  At 80% budget consumption, downgrade from a more expensive model to a
  cheaper one. The remaining_pct() method supports this decision.

USAGE:
  budget = RunBudget(max_tokens=8000, max_cost_usd=0.05)
  budget.consume(tokens=500, cost_usd=0.005)  # after each LLM call
  budget.tool_call()                            # before each tool call
  budget.check_elapsed()                        # periodically
  pct = budget.remaining_pct()                  # for routing / dashboard
"""

from __future__ import annotations

import time
from typing import Callable, Optional


class BudgetExceeded(Exception):
    """Raised when a run exceeds any of its budget limits.

    The supervisor catches this and transitions the run to FAILED.
    The exception carries the used and limits dicts for debugging."""

    def __init__(self, reason: str, used: dict, limits: dict):
        self.reason = reason
        self.used = used
        self.limits = limits
        super().__init__(
            f"Budget exceeded: {reason}. "
            f"Used: {used}. Limits: {limits}."
        )


class RunBudget:
    """
    Tracks tokens, cost, latency, and tool calls per run.

    Thread-safety: NOT thread-safe. Designed for use within a single
    asyncio event loop (the supervisor's loop). For multi-process
    budget tracking, persist to the DB (RunRecord.budget_used_* fields).
    """

    def __init__(
        self,
        max_tokens: int = 8000,
        max_cost_usd: float = 0.05,
        max_seconds: float = 60.0,
        max_tool_calls: int = 12,
        clock: Optional[Callable[[], float]] = None,
    ):
        """
        Args:
            max_tokens: maximum total tokens (input + output) per run.
            max_cost_usd: maximum total cost in USD per run.
            max_seconds: maximum wall-clock time per run (excluding
                approval wait — that's a separate 24h timeout).
            max_tool_calls: maximum number of tool calls per run.
            clock: injectable clock function (for tests). Default: time.monotonic.
                Using monotonic (not wall-clock) so it's immune to NTP adjustments.
        """
        self.max_tokens = max_tokens
        self.max_cost = max_cost_usd
        self.max_seconds = max_seconds
        self.max_tool_calls = max_tool_calls

        self.tokens_used = 0
        self.cost_usd = 0.0
        self.tool_calls = 0

        self._clock = clock or time.monotonic
        self.start_time = self._clock()

    # -------------------------------------------------------------------
    # Consumption methods — call these as the run progresses
    # -------------------------------------------------------------------

    def consume(self, tokens: int, cost_usd: float) -> None:
        """Record token and cost usage from an LLM call.

        Call this AFTER each LLM call with the token count and cost from
        the LLMResponse. Raises BudgetExceeded if over any limit."""
        self.tokens_used += tokens
        self.cost_usd += cost_usd
        self._check_exceeded()

    def tool_call(self) -> None:
        """Record a tool call. Call this BEFORE each tool invocation.

        Raises BudgetExceeded if over max_tool_calls."""
        self.tool_calls += 1
        self._check_exceeded()

    def check_elapsed(self) -> None:
        """Check if the wall-clock budget is exceeded. Call this periodically
        (e.g., at the top of every supervisor iteration).

        Raises BudgetExceeded if over max_seconds."""
        self._check_exceeded()

    # -------------------------------------------------------------------
    # Query methods — for dashboard / routing decisions
    # -------------------------------------------------------------------

    def elapsed_seconds(self) -> float:
        """How many seconds since the budget started (monotonic)."""
        return self._clock() - self.start_time

    def remaining_pct(self) -> float:
        """What fraction of the budget remains? (0.0 to 1.0)

        Returns the MINIMUM remaining across all four dimensions. The
        most-constrained dimension is the one that determines routing
        decisions (e.g., at 20% remaining, downgrade the model).
        """
        token_pct = max(0.0, 1.0 - self.tokens_used / self.max_tokens)
        cost_pct = max(0.0, 1.0 - self.cost_usd / self.max_cost)
        time_pct = max(0.0, 1.0 - self.elapsed_seconds() / self.max_seconds)
        tool_pct = max(0.0, 1.0 - self.tool_calls / self.max_tool_calls)
        return min(token_pct, cost_pct, time_pct, tool_pct)

    def to_dict(self) -> dict:
        """Serialize for persistence (RunRecord.budget_used_* fields).

        The supervisor calls this before each checkpoint to persist the
        current budget state. The dashboard reads from the persisted
        RunRecord, not from this live object."""
        return {
            "tokens_used": self.tokens_used,
            "cost_usd": round(self.cost_usd, 6),
            "tool_calls": self.tool_calls,
            "elapsed_seconds": round(self.elapsed_seconds(), 2),
            "remaining_pct": round(self.remaining_pct(), 4),
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost,
            "max_seconds": self.max_seconds,
            "max_tool_calls": self.max_tool_calls,
        }

    # -------------------------------------------------------------------
    # Internal: check all limits
    # -------------------------------------------------------------------

    def _check_exceeded(self) -> None:
        """Raise BudgetExceeded if ANY limit is exceeded."""
        used = {
            "tokens": self.tokens_used,
            "cost_usd": round(self.cost_usd, 6),
            "elapsed_seconds": round(self.elapsed_seconds(), 2),
            "tool_calls": self.tool_calls,
        }
        limits = {
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost,
            "max_seconds": self.max_seconds,
            "max_tool_calls": self.max_tool_calls,
        }

        if self.tokens_used > self.max_tokens:
            raise BudgetExceeded(
                f"tokens {self.tokens_used} > {self.max_tokens}",
                used, limits,
            )
        if self.cost_usd > self.max_cost:
            raise BudgetExceeded(
                f"cost ${self.cost_usd:.6f} > ${self.max_cost:.6f}",
                used, limits,
            )
        if self.elapsed_seconds() > self.max_seconds:
            raise BudgetExceeded(
                f"elapsed {self.elapsed_seconds():.1f}s > {self.max_seconds}s",
                used, limits,
            )
        if self.tool_calls > self.max_tool_calls:
            raise BudgetExceeded(
                f"tool_calls {self.tool_calls} > {self.max_tool_calls}",
                used, limits,
            )


__all__ = ["BudgetExceeded", "RunBudget"]
