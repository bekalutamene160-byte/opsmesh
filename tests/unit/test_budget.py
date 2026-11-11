"""
Tests for opsmesh.budget — RunBudget + BudgetExceeded.

Covers:
  - consume() tracks tokens and cost
  - tool_call() tracks tool calls
  - BudgetExceeded raised when tokens exceed
  - BudgetExceeded raised when cost exceeds
  - BudgetExceeded raised when tool calls exceed
  - BudgetExceeded raised when time exceeds
  - remaining_pct() returns minimum across dimensions
  - to_dict() serialization
  - Injectable clock for tests
"""

from __future__ import annotations

import pytest

from opsmesh.budget import BudgetExceeded, RunBudget


# ---------------------------------------------------------------------------
# 1. consume() tracks usage
# ---------------------------------------------------------------------------


class TestConsume:
    def test_tokens_accumulate(self):
        budget = RunBudget(max_tokens=1000)
        budget.consume(tokens=100, cost_usd=0.01)
        assert budget.tokens_used == 100
        budget.consume(tokens=200, cost_usd=0.02)
        assert budget.tokens_used == 300

    def test_cost_accumulates(self):
        budget = RunBudget(max_cost_usd=1.0)
        budget.consume(tokens=10, cost_usd=0.05)
        budget.consume(tokens=10, cost_usd=0.03)
        assert round(budget.cost_usd, 6) == 0.08

    def test_consume_does_not_raise_under_limit(self):
        budget = RunBudget(max_tokens=100, max_cost_usd=0.10)
        # Should not raise
        budget.consume(tokens=50, cost_usd=0.05)
        budget.consume(tokens=40, cost_usd=0.04)
        assert budget.tokens_used == 90


# ---------------------------------------------------------------------------
# 2. tool_call() tracks tool calls
# ---------------------------------------------------------------------------


class TestToolCall:
    def test_tool_calls_increment(self):
        budget = RunBudget(max_tool_calls=5)
        budget.tool_call()
        budget.tool_call()
        assert budget.tool_calls == 2

    def test_tool_call_under_limit_no_raise(self):
        budget = RunBudget(max_tool_calls=3)
        budget.tool_call()
        budget.tool_call()
        budget.tool_call()
        # 3 calls, limit 3 — should not raise
        # (exceeds is checked AFTER increment, so == is OK)


# ---------------------------------------------------------------------------
# 3. BudgetExceeded — tokens
# ---------------------------------------------------------------------------


class TestTokenBudgetExceeded:
    def test_tokens_exceeded_raises(self):
        budget = RunBudget(max_tokens=100)
        budget.consume(tokens=50, cost_usd=0.0)
        with pytest.raises(BudgetExceeded) as exc_info:
            budget.consume(tokens=60, cost_usd=0.0)  # total 110 > 100
        assert "tokens" in exc_info.value.reason
        assert exc_info.value.used["tokens"] == 110
        assert exc_info.value.limits["max_tokens"] == 100

    def test_tokens_exactly_at_limit_no_raise(self):
        """Consuming exactly the limit is OK (exceeds is > not >=)."""
        budget = RunBudget(max_tokens=100)
        budget.consume(tokens=100, cost_usd=0.0)
        # Should not raise — 100 is not > 100


# ---------------------------------------------------------------------------
# 4. BudgetExceeded — cost
# ---------------------------------------------------------------------------


class TestCostBudgetExceeded:
    def test_cost_exceeded_raises(self):
        budget = RunBudget(max_cost_usd=0.01)
        budget.consume(tokens=10, cost_usd=0.005)
        with pytest.raises(BudgetExceeded) as exc_info:
            budget.consume(tokens=10, cost_usd=0.01)  # total 0.015 > 0.01
        assert "cost" in exc_info.value.reason

    def test_cost_exactly_at_limit_no_raise(self):
        budget = RunBudget(max_cost_usd=0.10)
        budget.consume(tokens=10, cost_usd=0.10)
        # Should not raise — 0.10 is not > 0.10


# ---------------------------------------------------------------------------
# 5. BudgetExceeded — tool calls
# ---------------------------------------------------------------------------


class TestToolCallBudgetExceeded:
    def test_tool_calls_exceeded_raises(self):
        budget = RunBudget(max_tool_calls=2)
        budget.tool_call()
        budget.tool_call()
        with pytest.raises(BudgetExceeded) as exc_info:
            budget.tool_call()  # 3 > 2
        assert "tool_calls" in exc_info.value.reason


# ---------------------------------------------------------------------------
# 6. BudgetExceeded — time
# ---------------------------------------------------------------------------


class TestTimeBudgetExceeded:
    def test_time_exceeded_raises(self):
        """Using an injectable clock to simulate time passing."""
        current_time = [0.0]

        def fake_clock():
            return current_time[0]

        budget = RunBudget(max_seconds=10.0, clock=fake_clock)
        # Advance time past the limit
        current_time[0] = 15.0

        with pytest.raises(BudgetExceeded) as exc_info:
            budget.check_elapsed()
        assert "elapsed" in exc_info.value.reason
        assert exc_info.value.used["elapsed_seconds"] == 15.0

    def test_time_under_limit_no_raise(self):
        current_time = [0.0]

        def fake_clock():
            return current_time[0]

        budget = RunBudget(max_seconds=10.0, clock=fake_clock)
        current_time[0] = 5.0
        # Should not raise
        budget.check_elapsed()


# ---------------------------------------------------------------------------
# 7. remaining_pct()
# ---------------------------------------------------------------------------


class TestRemainingPct:
    def test_full_budget_at_start(self):
        """At the moment of creation, 100% of budget remains.
        Uses an injectable clock so no real time has elapsed."""
        fixed_time = [100.0]

        def fake_clock():
            return fixed_time[0]

        budget = RunBudget(max_tokens=100, max_cost_usd=0.10,
                           max_seconds=60.0, max_tool_calls=10, clock=fake_clock)
        assert budget.remaining_pct() == 1.0

    def test_decreases_with_consumption(self):
        budget = RunBudget(max_tokens=100, max_cost_usd=0.10,
                           max_seconds=60.0, max_tool_calls=10)
        budget.consume(tokens=50, cost_usd=0.0)
        # Token dimension is at 50%, others at 100% → min is 50%
        assert budget.remaining_pct() == 0.5

    def test_returns_minimum_across_dimensions(self):
        """remaining_pct returns the MINIMUM across all dimensions — the
        most constrained one is what matters for routing decisions."""
        budget = RunBudget(max_tokens=1000, max_cost_usd=0.10,
                           max_seconds=60.0, max_tool_calls=10)
        # Consume most of the cost budget
        budget.consume(tokens=10, cost_usd=0.09)
        # Cost is at 10% remaining, tokens at 99% → min is 10%
        assert budget.remaining_pct() < 0.11

    def test_zero_when_exceeded(self):
        """After BudgetExceeded, remaining is 0."""
        budget = RunBudget(max_tokens=100)
        try:
            budget.consume(tokens=200, cost_usd=0.0)
        except BudgetExceeded:
            pass
        assert budget.remaining_pct() == 0.0


# ---------------------------------------------------------------------------
# 8. to_dict() serialization
# ---------------------------------------------------------------------------


class TestToDict:
    def test_contains_all_fields(self):
        budget = RunBudget(
            max_tokens=8000, max_cost_usd=0.05,
            max_seconds=60.0, max_tool_calls=12,
        )
        budget.consume(tokens=500, cost_usd=0.005)
        budget.tool_call()

        data = budget.to_dict()
        assert data["tokens_used"] == 500
        assert data["cost_usd"] == 0.005
        assert data["tool_calls"] == 1
        assert "elapsed_seconds" in data
        assert "remaining_pct" in data
        assert data["max_tokens"] == 8000
        assert data["max_cost_usd"] == 0.05
        assert data["max_seconds"] == 60.0
        assert data["max_tool_calls"] == 12

    def test_cost_usd_rounded(self):
        """Cost is rounded to 6 decimal places to avoid float artifacts."""
        budget = RunBudget(max_cost_usd=1.0)
        budget.consume(tokens=1, cost_usd=0.000001)
        budget.consume(tokens=1, cost_usd=0.000001)
        data = budget.to_dict()
        assert data["cost_usd"] == 0.000002


# ---------------------------------------------------------------------------
# 9. Defaults match Engineering Contract
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_max_tokens(self):
        budget = RunBudget()
        assert budget.max_tokens == 8000

    def test_default_max_cost(self):
        budget = RunBudget()
        assert budget.max_cost == 0.05

    def test_default_max_seconds(self):
        budget = RunBudget()
        assert budget.max_seconds == 60.0

    def test_default_max_tool_calls(self):
        budget = RunBudget()
        assert budget.max_tool_calls == 12
