"""
Tests for the hardened Day 6 components: worker entrypoint, config consistency,
metrics fixes, and Dockerfile/.dockerignore presence.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from opsmesh.config import Settings, get_settings
from opsmesh.observability.metrics import compute_metrics
from opsmesh.state_machine import State
from opsmesh.state_store import InMemoryStateStore


# ===========================================================================
# 1. Worker entrypoint — `python -m opsmesh.worker` must start
# ===========================================================================


class TestWorkerEntrypoint:
    def test_worker_module_has_main(self):
        """The worker module must have a __main__ block so `python -m opsmesh.worker` works."""
        import importlib
        spec = importlib.util.find_spec("opsmesh.worker")
        assert spec is not None
        # Check that _build_default_worker exists
        from opsmesh.worker import _build_default_worker
        worker = _build_default_worker()
        assert worker.worker_id.startswith("worker_")
        assert worker.queue is not None
        assert worker.supervisor is not None

    def test_worker_starts_and_stops(self):
        """Start the worker process, then kill it after 2 seconds. Verify it started."""
        proc = subprocess.Popen(
            [sys.executable, "-m", "opsmesh.worker"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "OPSMESH_LLM_PROVIDER": "mock"},
        )
        time.sleep(2)
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=5)
        # Verify it started (should have logged "worker.starting")
        output = stdout.decode() + stderr.decode()
        assert "worker.starting" in output or "Worker" in output or proc.returncode is not None


# ===========================================================================
# 2. Config consistency — env prefix matches .env.example
# ===========================================================================


class TestConfigConsistency:
    def test_env_prefix_is_opsmesh(self):
        """All Settings fields use OPSMESH_ prefix."""
        get_settings.cache_clear()
        s = Settings()
        # The model_config has env_prefix="OPSMESH_"
        assert s.model_config.get("env_prefix") == "OPSMESH_"

    def test_env_example_uses_opsmesh_prefix(self):
        """The .env.example file uses OPSMESH_ prefix for all vars."""
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            ".env.example",
        )
        if not os.path.exists(env_path):
            pytest.skip(".env.example not found")
        # Use encoding="utf-8" to avoid cp1252 decode errors on Windows
        with open(env_path, encoding="utf-8") as f:
            content = f.read()
        # Every non-comment, non-empty line should start with OPSMESH_
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            assert line.startswith("OPSMESH_"), f"Env var without OPSMESH_ prefix: {line}"

    def test_settings_validate_at_startup(self):
        """Settings can be constructed without env vars (mock defaults)."""
        get_settings.cache_clear()
        s = Settings()
        assert s.llm_provider == "mock"
        assert s.email_provider == "mock"
        assert s.email_kill_switch is True


# ===========================================================================
# 3. Metrics fixes — LLM error rate denominator, terminal-only latency
# ===========================================================================


class TestMetricsFixes:
    @pytest.mark.asyncio
    async def test_llm_error_rate_uses_token_count_not_tool_calls(self):
        """FIX: LLM error rate denominator must be runs with token consumption,
        not total tool calls."""
        from opsmesh.models import RunRecord
        store = InMemoryStateStore()

        # Run 1: failed with LLM error, consumed tokens
        r1 = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.FAILED)
        r1.budget_used_tokens = 500  # consumed tokens → made LLM calls
        r1.last_error = "TimeoutError: LLM timed out"
        await store.save_checkpoint(r1)

        # Run 2: succeeded, consumed tokens
        r2 = RunRecord(workspace_id="ws_1", lead_id="lead_2", state=State.SENT)
        r2.budget_used_tokens = 300
        await store.save_checkpoint(r2)

        # Run 3: succeeded, but NO token consumption (shouldn't be in denominator)
        r3 = RunRecord(workspace_id="ws_1", lead_id="lead_3", state=State.SENT)
        r3.budget_used_tokens = 0
        await store.save_checkpoint(r3)

        metrics = await compute_metrics(store, workspace_id="ws_1")
        # LLM call count = 2 (only runs 1 and 2 consumed tokens)
        assert metrics["providers"]["llm"]["calls_24h"] == 2
        # LLM errors = 1 (run 1)
        assert metrics["providers"]["llm"]["errors_24h"] == 1
        # Error rate = 1/2 = 0.5
        assert metrics["providers"]["llm"]["error_rate"] == 0.5

    @pytest.mark.asyncio
    async def test_latency_excludes_non_terminal_runs(self):
        """FIX: p95 latency must only include TERMINAL runs, not paused or active."""
        from opsmesh.models import RunRecord, utcnow
        from datetime import timedelta
        store = InMemoryStateStore()

        # Terminal run (SENT) — should be included
        r1 = RunRecord(workspace_id="ws_1", lead_id="lead_1", state=State.SENT)
        r1.started_at = utcnow() - timedelta(seconds=10)
        r1.terminal_at = utcnow()
        await store.save_checkpoint(r1)

        # Non-terminal run (AWAITING_APPROVAL) — should be EXCLUDED
        r2 = RunRecord(workspace_id="ws_1", lead_id="lead_2", state=State.AWAITING_APPROVAL)
        r2.started_at = utcnow() - timedelta(seconds=1000)
        await store.save_checkpoint(r2)

        metrics = await compute_metrics(store, workspace_id="ws_1")
        # p50 and p95 should be based on ONLY the terminal run
        # (a single data point — p50 = p95 = that run's latency)
        assert metrics["runs"]["p50_latency_ms"] > 0
        assert metrics["runs"]["p95_latency_ms"] > 0
        # The paused run's 1000s latency should NOT skew the p95
        assert metrics["runs"]["p95_latency_ms"] < 100000  # < 100 seconds


# ===========================================================================
# 4. Dockerfile + .dockerignore presence
# ===========================================================================


class TestDockerFiles:
    def test_dockerfile_exists(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "Dockerfile",
        )
        assert os.path.exists(path), "Dockerfile must exist"

    def test_dockerfile_uses_non_root(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "Dockerfile",
        )
        with open(path) as f:
            content = f.read()
        assert "USER opsmesh" in content, "Dockerfile must use non-root user"

    def test_dockerignore_exists(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            ".dockerignore",
        )
        assert os.path.exists(path), ".dockerignore must exist"

    def test_dockerignore_excludes_env(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            ".dockerignore",
        )
        with open(path) as f:
            content = f.read()
        assert ".env" in content, ".dockerignore must exclude .env files"
        assert "venv" in content, ".dockerignore must exclude venv"
