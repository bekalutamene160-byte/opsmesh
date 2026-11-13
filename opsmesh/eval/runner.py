"""
OPSMESH Eval Runner — runs benchmark scenarios against the system.

Architecture (course Ch 11):
  The eval strategy follows four principles:
    1. Deterministic assertions for states, transitions, permissions, idempotency.
    2. LLM-as-judge only for qualitative critique quality (Day 7).
    3. Reproducibility — same inputs → same outputs (via mock provider).
    4. Every discovered defect leads to a regression test.

Day 3 scope:
  - 10 scenarios that test the reliability primitives (state machine, budget,
    checkpoint/resume).
  - Uses mock specialists (deterministic, no API keys needed).
  - Asserts on final state.
  - Writes results to JSONL.

Day 7 will add:
  - The remaining 14 scenarios (adversarial, failure-recovery).
  - LLM-judge for qualitative critique quality.
  - Full pipeline integration (sending, CRM, reconciliation).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from opsmesh.eval.mocks import make_eval_specialists
from opsmesh.models import ResearchResult, RunRecord, utcnow
from opsmesh.state_machine import State
from opsmesh.state_store import InMemoryStateStore
from opsmesh.supervisor import Supervisor


class EvalResult:
    """Result of running one eval scenario."""

    def __init__(
        self,
        scenario_id: str,
        scenario_name: str,
        passed: bool,
        expected_state: str,
        actual_state: str,
        duration_ms: float,
        error: Optional[str] = None,
    ):
        self.scenario_id = scenario_id
        self.scenario_name = scenario_name
        self.passed = passed
        self.expected_state = expected_state
        self.actual_state = actual_state
        self.duration_ms = duration_ms
        self.error = error

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "scenario_name": self.scenario_name,
            "passed": self.passed,
            "expected_state": self.expected_state,
            "actual_state": self.actual_state,
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
        }

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict())


class EvalRunner:
    """
    Runs benchmark scenarios against the system.

    Usage:
        runner = EvalRunner(scenarios_path="opsmesh/eval/scenarios.jsonl")
        summary = await runner.run_all()
        print(summary)
        # Save results:
        runner.save_results("eval_results.jsonl")
    """

    def __init__(self, scenarios_path: str = "opsmesh/eval/scenarios.jsonl"):
        self.scenarios_path = Path(scenarios_path)
        self.results: list[EvalResult] = []

    def load_scenarios(self) -> list[dict]:
        """Load scenarios from the JSONL file."""
        scenarios = []
        with open(self.scenarios_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                scenarios.append(json.loads(line))
        return scenarios

    async def run_all(self, verbose: bool = False) -> dict:
        """Run all scenarios. Returns a summary dict.

        Returns:
            {
                "total": 10,
                "passed": 9,
                "failed": 1,
                "pass_rate": 0.9,
                "results": [...]
            }
        """
        scenarios = self.load_scenarios()
        self.results = []

        for scenario in scenarios:
            result = await self._run_scenario(scenario)
            self.results.append(result)
            if verbose:
                status = "PASS" if result.passed else "FAIL"
                print(
                    f"  [{status}] {result.scenario_id}: {result.scenario_name} "
                    f"(expected={result.expected_state}, actual={result.actual_state})"
                )

        passed = sum(1 for r in self.results if r.passed)
        total = len(self.results)
        return {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": passed / total if total > 0 else 0.0,
            "results": [r.to_dict() for r in self.results],
        }

    async def _run_scenario(self, scenario: dict) -> EvalResult:
        """Run one scenario and check the result."""
        import time
        start = time.monotonic()

        scenario_id = scenario["id"]
        scenario_name = scenario["name"]
        expected_state = scenario["expected_state"]
        mock_config = scenario.get("mock_config", {})
        budget_override = scenario.get("budget_override")
        pre_state = scenario.get("pre_state")

        try:
            # Fresh environment for each scenario
            store = InMemoryStateStore()
            specialists = make_eval_specialists(mock_config)

            # Build a real ApprovalGate so Gate 2 (email send) can be exercised.
            # Without this, the supervisor pauses at awaiting_approval and the
            # send step never runs — so any scenario expecting sent /
            # send_unknown / failed-during-send would fail. The runner
            # auto-approves Gate 2 below to simulate the human decision.
            from opsmesh.approval_gate import ApprovalGate
            from opsmesh.approval_store import InMemoryApprovalStore
            approval_store = InMemoryApprovalStore()
            gate = ApprovalGate(store=store, approval_store=approval_store)

            # Create a run
            run = RunRecord(
                workspace_id="ws_eval",
                lead_id="lead_eval",
                state=State.NEW,
            )

            # Handle pre_state (simulate a crash mid-run)
            if pre_state == "qualifying_with_research_done":
                run.state = State.QUALIFYING
                run.research = ResearchResult(
                    evidence=[],
                    summary="Pre-existing research.",
                    sufficient_for_qualification=True,
                )

            await store.save_checkpoint(run)

            # Run the supervisor
            budget_factory = None
            if budget_override:
                from opsmesh.budget import RunBudget
                budget_factory = lambda: RunBudget(**budget_override)

            supervisor = Supervisor(
                specialists=specialists, store=store,
                approvals=gate,
                budget_factory=budget_factory,
            )

            # Create a job-like object
            class EvalJob:
                run_id = run.run_id
                workspace_id = run.workspace_id
                lead_id = run.lead_id

            result_run = await supervisor.run(EvalJob())

            # Conditionally exercise the Gate 2 send step. By default the run
            # pauses at awaiting_approval (the human gate — which is what most
            # scenarios assert). Scenarios that need the send step to actually
            # fire set "auto_approve_gate2": true; the runner simulates the
            # human "approve" decision and re-runs the supervisor so its
            # resume logic transitions to SENDING and the SendingSpecialist
            # runs. Scenarios expecting "rejected" get an auto-REJECT instead.
            auto_decision = scenario.get("auto_approve_gate2", False)
            wants_reject = (expected_state == "rejected")
            if (auto_decision or wants_reject) and result_run.state == State.AWAITING_APPROVAL \
                    and result_run.pending_approval_id:
                try:
                    await gate.decide(
                        approval_id=result_run.pending_approval_id,
                        workspace_id="ws_eval",
                        decision="reject" if wants_reject else "approve",
                        decided_by="eval_auto",
                    )
                    result_run = await supervisor.run(EvalJob())
                except Exception as decide_err:
                    duration_ms = (time.monotonic() - start) * 1000
                    return EvalResult(
                        scenario_id, scenario_name, False,
                        expected_state, result_run.state.value, duration_ms,
                        error=f"auto-decide failed: {type(decide_err).__name__}: {str(decide_err)[:150]}",
                    )

            actual_state = result_run.state.value

            # Check special assertions
            if "expected_side_effects" in scenario:
                side_effects = scenario["expected_side_effects"]
                if "research_call_count" in side_effects:
                    expected_count = side_effects["research_call_count"]
                    actual_count = specialists["researching"].call_count
                    if actual_count != expected_count:
                        duration_ms = (time.monotonic() - start) * 1000
                        return EvalResult(
                            scenario_id, scenario_name, False,
                            expected_state, actual_state, duration_ms,
                            error=f"research_call_count: expected {expected_count}, got {actual_count}",
                        )

            duration_ms = (time.monotonic() - start) * 1000
            passed = (actual_state == expected_state)
            error = None if passed else f"Expected {expected_state}, got {actual_state}"
            return EvalResult(
                scenario_id, scenario_name, passed,
                expected_state, actual_state, duration_ms, error,
            )

        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            return EvalResult(
                scenario_id, scenario_name, False,
                expected_state, "exception", duration_ms,
                error=f"{type(e).__name__}: {str(e)[:200]}",
            )

    def save_results(self, output_path: str) -> None:
        """Save results to a JSONL file."""
        with open(output_path, "w") as f:
            for result in self.results:
                f.write(result.to_jsonl() + "\n")

    def print_summary(self, summary: dict) -> None:
        """Print a human-readable summary."""
        print(f"\n{'='*60}")
        print(f"OPSMESH Eval Results")
        print(f"{'='*60}")
        print(f"Total:   {summary['total']}")
        print(f"Passed:  {summary['passed']}")
        print(f"Failed:  {summary['failed']}")
        print(f"Rate:    {summary['pass_rate']:.1%}")
        print(f"{'='*60}")

        for result in self.results:
            status = "✓ PASS" if result.passed else "✗ FAIL"
            print(f"  {status}  {result.scenario_id}: {result.scenario_name}")
            if not result.passed and result.error:
                print(f"           → {result.error}")

        print(f"{'='*60}")


async def main():
    """Run the eval suite from the command line.

    Supports both flag and positional forms:
        python -m opsmesh.eval.runner                          # defaults
        python -m opsmesh.eval.runner --scenarios=all          # explicit
        python -m opsmesh.eval.runner --output=eval.jsonl
        python -m opsmesh.eval.runner path/to/scenarios.jsonl  # positional
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="opsmesh.eval.runner",
        description="Run the OPSMESH eval suite (32 scenarios, mock providers).",
    )
    parser.add_argument(
        "scenarios_pos",
        nargs="?",
        default=None,
        help="Path to scenarios JSONL (positional form). Optional.",
    )
    parser.add_argument(
        "--scenarios",
        default=None,
        help='Path to scenarios JSONL, or "all" for the bundled suite.',
    )
    parser.add_argument(
        "--output",
        default="eval_results.jsonl",
        help="Where to write the JSONL results (default: eval_results.jsonl).",
    )
    args = parser.parse_args()

    # Resolve scenarios path: flag wins, then positional, then default.
    scenarios_path = args.scenarios or args.scenarios_pos or "opsmesh/eval/scenarios.jsonl"
    if scenarios_path == "all":
        scenarios_path = "opsmesh/eval/scenarios.jsonl"

    runner = EvalRunner(scenarios_path=scenarios_path)
    summary = await runner.run_all(verbose=True)
    runner.save_results(args.output)
    runner.print_summary(summary)

    # Exit code: 0 if all passed, 1 if any failed
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    import asyncio
    import sys

    result_code = asyncio.run(main())
    sys.exit(result_code)


__all__ = ["EvalResult", "EvalRunner"]
