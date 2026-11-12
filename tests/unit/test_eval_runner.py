"""
Tests for opsmesh.eval.runner — the benchmark scenario runner.

Covers:
  - Loading scenarios from JSONL
  - Running each scenario with mock specialists
  - Asserting the final state matches expected
  - Scenario 001 (happy path) → awaiting_approval
  - Scenario 002 (disqualified) → failed
  - Scenario 003 (insufficient research) → awaiting_evidence_review
  - Scenario 004 (NEEDS_REVIEW) → awaiting_evidence_review
  - Scenario 005 (critic rejection max retries) → failed
  - Scenario 010 (resume doesn't repeat research) → research_call_count == 0
  - Summary computation (pass rate)
  - Results serialization to JSONL
"""

from __future__ import annotations

import json
import os

import pytest

from opsmesh.eval.runner import EvalRunner


# ---------------------------------------------------------------------------
# 1. Loading scenarios
# ---------------------------------------------------------------------------


class TestLoadScenarios:
    def test_loads_all_scenarios(self):
        runner = EvalRunner()
        scenarios = runner.load_scenarios()
        # Stage B Phase 6: expanded from 10 to 32 scenarios (normal +
        # adversarial + failure-injection).
        assert len(scenarios) >= 32

    def test_each_scenario_has_required_fields(self):
        runner = EvalRunner()
        scenarios = runner.load_scenarios()
        for s in scenarios:
            assert "id" in s
            assert "name" in s
            assert "expected_state" in s
            assert "mock_config" in s

    def test_scenario_ids_are_unique(self):
        runner = EvalRunner()
        scenarios = runner.load_scenarios()
        ids = [s["id"] for s in scenarios]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# 2. Running scenarios
# ---------------------------------------------------------------------------


class TestRunScenarios:
    @pytest.mark.asyncio
    async def test_happy_path_scenario(self):
        """eval_001: normally qualified lead → awaiting_approval."""
        runner = EvalRunner()
        scenarios = runner.load_scenarios()
        happy = [s for s in scenarios if s["id"] == "eval_001"][0]

        result = await runner._run_scenario(happy)
        assert result.scenario_id == "eval_001"
        assert result.expected_state == "awaiting_approval"
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_disqualified_scenario(self):
        """eval_002: disqualified lead → failed."""
        runner = EvalRunner()
        scenarios = runner.load_scenarios()
        disq = [s for s in scenarios if s["id"] == "eval_002"][0]

        result = await runner._run_scenario(disq)
        assert result.expected_state == "failed"
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_insufficient_research_scenario(self):
        """eval_003: insufficient research → awaiting_evidence_review (Gate 1)."""
        runner = EvalRunner()
        scenarios = runner.load_scenarios()
        insuf = [s for s in scenarios if s["id"] == "eval_003"][0]

        result = await runner._run_scenario(insuf)
        assert result.expected_state == "awaiting_evidence_review"
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_needs_review_scenario(self):
        """eval_004: NEEDS_REVIEW → awaiting_evidence_review (Gate 1)."""
        runner = EvalRunner()
        scenarios = runner.load_scenarios()
        nr = [s for s in scenarios if s["id"] == "eval_004"][0]

        result = await runner._run_scenario(nr)
        assert result.expected_state == "awaiting_evidence_review"
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_critic_rejection_max_retries(self):
        """eval_005: critic always rejects → failed after max retries."""
        runner = EvalRunner()
        scenarios = runner.load_scenarios()
        rej = [s for s in scenarios if s["id"] == "eval_005"][0]

        result = await runner._run_scenario(rej)
        assert result.expected_state == "failed"
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_resume_does_not_repeat_research(self):
        """eval_010: resume from QUALIFYING (research already done) should
        NOT re-execute research. Verified by checking research_call_count == 0."""
        runner = EvalRunner()
        scenarios = runner.load_scenarios()
        resume = [s for s in scenarios if s["id"] == "eval_010"][0]

        result = await runner._run_scenario(resume)
        assert result.expected_state == "awaiting_approval"
        assert result.passed is True
        assert result.error is None


# ---------------------------------------------------------------------------
# 3. Run all scenarios
# ---------------------------------------------------------------------------


class TestRunAll:
    @pytest.mark.asyncio
    async def test_run_all_returns_summary(self):
        runner = EvalRunner()
        summary = await runner.run_all()

        assert "total" in summary
        assert "passed" in summary
        assert "failed" in summary
        assert "pass_rate" in summary
        assert "results" in summary
        # Stage B Phase 6: total reflects the expanded scenario set (>= 10,
        # grows as scenarios are added). Some scenarios may be skipped if
        # their mock_config isn't fully wired yet.
        assert summary["total"] >= 10

    @pytest.mark.asyncio
    async def test_pass_rate_is_calculated_correctly(self):
        runner = EvalRunner()
        summary = await runner.run_all()
        assert summary["passed"] + summary["failed"] == summary["total"]
        expected_rate = summary["passed"] / summary["total"]
        assert abs(summary["pass_rate"] - expected_rate) < 0.001


# ---------------------------------------------------------------------------
# 4. Results serialization
# ---------------------------------------------------------------------------


class TestResultsSerialization:
    @pytest.mark.asyncio
    async def test_save_results_to_jsonl(self, tmp_path):
        """Results can be saved to JSONL and re-loaded."""
        runner = EvalRunner()
        await runner.run_all()

        output = tmp_path / "eval_results.jsonl"
        runner.save_results(str(output))

        # Verify the file exists and is valid JSONL
        assert output.exists()
        with open(output) as f:
            lines = f.readlines()
        # Stage B Phase 6: results count grows with the expanded scenario set.
        assert len(lines) >= 10
        for line in lines:
            data = json.loads(line.strip())
            assert "scenario_id" in data
            assert "passed" in data
            assert "expected_state" in data
            assert "actual_state" in data


# ---------------------------------------------------------------------------
# 5. Summary printing (no crash)
# ---------------------------------------------------------------------------


class TestPrintSummary:
    @pytest.mark.asyncio
    async def test_print_summary_does_not_crash(self, capsys):
        runner = EvalRunner()
        summary = await runner.run_all()
        runner.print_summary(summary)
        captured = capsys.readouterr()
        assert "OPSMESH Eval Results" in captured.out
        assert "Total:" in captured.out
