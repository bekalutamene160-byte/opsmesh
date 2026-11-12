# OPSMESH Baseline Test Results

**Audit date:** 2026-11-12
**Repo:** `/home/z/my-project/opsmesh/` (branch `main`, commit `4ec6cdf`)
**Python:** 3.12.16 (CPython, Clang 23.1.3)

This document records the exact command, dependency versions, test count, failures and warnings from running the existing OPSMESH test suite in a clean environment. The README (`README.md:91`, `:200`) claims **"739 passed"** — this audit verifies that claim.

---

## 1. The README claim

From `README.md:197-198`:
```
python -m pytest tests/ -q --ignore=tests/integration
# 739 passed
```

This is a **claim to verify, not an established fact**. The mandate requires running the suite in a clean environment.

---

## 2. First attempt: clean environment, no extra deps installed

**Command:**
```
python -m pytest tests/ -q --ignore=tests/integration --tb=short -p no:cacheprovider
```

**Result: collection interrupted — 8 errors, 0 tests run.**

```
ERROR collecting tests/unit/test_day6.py
ERROR collecting tests/unit/test_lead_intake.py
ERROR collecting tests/unit/test_phase3_api.py
ERROR collecting tests/unit/test_phase4_api.py
ERROR collecting tests/unit/test_phase5_frontend.py
ERROR collecting tests/unit/test_phase6_auth.py
ERROR collecting tests/unit/test_phase7_database.py
ERROR collecting tests/unit/test_sqlite_stores.py
!!!!!!!!!!!!!!!!!!! Interrupted: 8 errors during collection !!!!!!!!!!!!!!!!!!!!
8 errors in 0.79s
```

**Root cause:** the clean environment was missing runtime dependencies that `requirements.txt` declares but were not installed in the sandbox venv:

- `aiosqlite` (imported at `opsmesh/adapters/db/connection.py:41`)
- `sqlalchemy`, `alembic`, `asyncpg`
- `structlog`, `PyJWT`, `cryptography`
- `google-generativeai`, `python-multipart`, `itsdangerous`, `jinja2`, `email-validator`
- `opentelemetry-api`, `opentelemetry-sdk`, `pytest-cov`, `mcp`

**Severity: P2 (dependency hygiene).** `requirements.txt` is the source of truth for the deploy build (`render.yaml` buildCommand: `pip install -r requirements.txt`), so a real Render build would install these and the suite would collect. But the **pinned versions in `requirements.txt` are partially invalid for the current sandbox** — see `DEPENDENCY_MAP.md` §3 for the version-drift table (e.g. `sqlalchemy==2.1.8` is not a release that pip could resolve; we installed `sqlalchemy>=2.0,<2.1` instead).

---

## 3. Second attempt: deps installed, full suite

**Install command (sandbox venv, no `--user`):**
```
python -m pip install aiosqlite asyncpg "sqlalchemy>=2.0,<2.1" alembic \
  structlog PyJWT cryptography "google-generativeai>=0.8" python-multipart \
  itsdangerous jinja2 email-validator opentelemetry-api opentelemetry-sdk \
  pytest-cov mcp
```

**Test command (exact, per README):**
```
python -m pytest tests/ -q --ignore=tests/integration --tb=line -p no:cacheprovider
```

**Result:**
```
........................................................................ [  9%]
........................................................................ [ 19%]
........................................................................ [ 29%]
........................................................................ [ 38%]
........................................................................ [ 48%]
........................................................................ [ 58%]
........................................................................ [ 68%]
........................................................................ [ 77%]
........................................................................ [ 87%]
........................................................................ [ 97%]
...................                                                      [100%]
739 passed in 10.94s
```

### Summary

| Metric | Value |
|---|---|
| Tests run | **739** |
| Passed | **739** |
| Failed | **0** |
| Errored | **0** |
| Skipped | **0** |
| Warnings | **0** |
| Wall time | 10.94s (first run) / 10.63s (repeat) |
| Collection errors | 0 (after dep install) |
| Coverage | not measured in this run (Makefile `test` target enforces `--cov-fail-under=80`; not invoked here) |

**Verdict: README claim "739 passed" is VERIFIED.** The suite is green once the declared dependencies are installed.

---

## 4. Test file inventory

| Path | Files | Notes |
|---|---|---|
| `tests/unit/` | 31 | unit tests, no external deps; uses mocks |
| `tests/integration/` | 2 | `test_api.py`, `test_real_providers.py` — require real API keys (Groq, Exa, Resend, HubSpot); **excluded** from the baseline run via `--ignore=tests/integration` |
| `tests/failure/` | 1 | `test_failure_injection.py` |
| `tests/conftest.py` | 1 | shared fixtures |
| `tests/fixtures/llm/` | dir | LLM response fixtures |
| **Total `def test_` definitions** | **743** | (function count; 739 collect+pass, 4 may be parametrized variants or conditionally skipped) |

`pyproject.toml` markers: `unit`, `integration`, `security`, `failure`, `eval`. `addopts = "-ra --strict-markers"`. `asyncio_mode = "auto"`.

---

## 5. What the suite does NOT cover (gaps the mandate requires)

Running 739 green tests is not proof of production readiness. The mandate's Stage C acceptance criteria require coverage the current suite does **not** provide. These are documented as gaps, not as failures:

### Workflow & recovery
- **No end-to-end test of the full lead → CRM pipeline through the real worker loop.** Tests exercise specialists and routes in isolation; there is no test that drives `Tally intake → durable lead → durable job → research → qualify → Gate 1 → draft → critic → Gate 2 → suppression → ToolGateway → Resend → delivery status → HubSpot outbox → CRM sync → dashboard` as one flow.
- **No worker restart / recovery test.** The mandate requires "test restart and recovery at every meaningful boundary." The current suite has no test that claims a job, kills the worker mid-step, restarts, and verifies the lease expires and the job is reclaimed.
- **No stale-job recovery / DLQ test.** `max_attempts=4` (`job_queue.py:41`) and `dead_letter()` exist but are not exercised at the queue level.

### ToolGateway & approval invariants
- **`test_tool_gateway.py` unit-tests the gateway in isolation**, but there is **no test that the production supervisor path actually routes side effects through it** — because it doesn't (see `CRITICAL_FINDINGS.md` P0-1). The bypass is untested because it is the actual behavior.
- **No test that editing an approved payload invalidates the approval at the send site.** The hash is computed (`supervisor.py:554`) and the verifier exists (`approval_store.py:325`), but the verifier is never called in production, so no test catches a modified-payload send.

### Failure injection
- `tests/failure/test_failure_injection.py` exists (1 file) but the mandate requires 30+ scenarios across normal/insufficient-research/conflicting-evidence/prompt-injection/malicious-URLs/duplicate-webhooks/duplicate-tool-calls/invalid-approval/expired-approval/modified-payload/rejected-email/email-timeout/SEND_UNKNOWN-recovery/CRM-timeout/CRM-duplicate/database-failure/worker-restart/queue-recovery/budget-exhaustion/rate-limiting/workspace-boundary/ToolGateway-bypass/provider-outage. **The current failure suite is far below 30 scenarios** — see `STAGE_A_PLAN.md` Phase 6 for the expansion plan.

### Security
- No test that the Tally webhook **rejects** unsigned POSTs in production mode (currently it accepts them — `CRITICAL_FINDINGS.md` P0-2).
- No test that `/dashboard` or `/metrics` require auth (currently they don't — P1-3).
- No test that `POST /leads` or `/demo/simulate` are auth-guarded (currently they aren't — P1-3).
- No test that Resend sends are restricted to a configured test recipient (no such restriction exists — P0-3).
- No test that HubSpot mutations are disabled in demo mode (no such flag exists — P0-4).
- No cross-workspace isolation test (workspace is hardcoded `"default"` everywhere — P1-3).

### Observability
- No test that p50/p95/p99 are computed correctly, or that the dashboard labels "insufficient data" when sample size is small (the mandate explicitly calls out the "100% success rate from one run" anti-pattern).

### Migrations
- Only **1 Alembic migration** exists (`001_initial_schema.py`). There is **no migration test** that verifies `alembic upgrade head` is idempotent or that downgrade is safe. The runtime schema is actually created by `connection.py:_init_sqlite_schema` (`SCHEMA_SQLITE`/`SCHEMA_POSTGRES`), not by Alembic — schema drift between Alembic and the runtime DDL is untested.

---

## 6. Linting and type-checking

The Makefile defines `lint: ruff check . && mypy --strict opsmesh`. These were **not run** in this baseline because `ruff` and `mypy` are not installed in the sandbox venv and are not in `requirements.txt` (they are dev dependencies that the Makefile assumes are present). 

**Recommendation for Stage B:** add a `requirements-dev.txt` or `[project.optional-dependencies] dev = [...]` block so `make lint` works in a clean checkout. This is logged as a P3 hygiene finding in `CRITICAL_FINDINGS.md`.

---

## 7. Reproduction

To reproduce the baseline test result in a clean environment:

```bash
cd /home/z/my-project/opsmesh

# 1. Install runtime + test deps (requirements.txt pins may need relaxing — see DEPENDENCY_MAP.md)
python -m pip install -r requirements.txt
# If sqlalchemy==2.1.8 fails to resolve, install: "sqlalchemy>=2.0,<2.1"

# 2. Run the unit suite (matches README)
python -m pytest tests/ -q --ignore=tests/integration
# Expected: 739 passed in ~11s

# 3. (Optional) Run integration tests with real keys
OPSMESH_LLM_API_KEY=gsk_... OPSMESH_EXA_API_KEY=... \
OPSMESH_EMAIL_API_KEY=re_... OPSMESH_CRM_API_KEY=pat_... \
python -m pytest tests/integration/ -v

# 4. (Optional) Coverage gate (Makefile target)
python -m pytest tests/ --ignore=tests/integration --cov=opsmesh --cov-fail-under=80
```

**Log artifacts:** the raw pytest outputs are preserved in `docs/audit/.test_run.log` (failed collection, clean env), `docs/audit/.test_run2.log` (failed `--user` install attempt), and `docs/audit/.test_run3.log` (successful 739-passed run). These are excluded from the zip deliverable as transient scratch files.
