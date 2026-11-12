# OPSMESH Baseline Inventory

**Audit date:** 2026-11-12
**Audited revision:** working tree at `/home/z/my-project/opsmesh/` (branch `main`, latest commit `4ec6cdf`)
**Scope:** Phase 0 read-only inventory — no application code modified.

This document is a complete inventory of every source module, route, database store, provider adapter, worker, frontend page, migration, test and deployment file in the OPSMESH repository. It is the authoritative starting point for the Stage A audit. Findings with severity ratings live in `CRITICAL_FINDINGS.md`; deployment and dependency specifics live in `DEPLOYMENT_BASELINE.md` and `DEPENDENCY_MAP.md`.

---

## 1. Repository shape

| Metric | Value |
|---|---|
| Top-level Python package | `opsmesh/` |
| Python files (source) | ~129 |
| Test files | 33 (31 in `tests/unit/`, 2 in `tests/integration/`, 1 in `tests/failure/`) |
| `def test_` definitions | 743 (function-definition count) |
| README claim | "739 passed" (`README.md:91`, `:200`) — **not verifiable in clean env**, see `BASELINE_TEST_RESULTS.md` |
| Frontend | Vanilla HTML/CSS/JS, no build step, mounted at `/ui` |
| Alembic migrations | 1 (`001_initial_schema.py`) |
| Deployment | Render blueprint (`render.yaml`) + `Dockerfile` + `Procfile` |

---

## 2. Source package layout

```
opsmesh/
├── __init__.py:3            # __version__ = "0.1.0"
├── config.py                # pydantic-settings Settings + get_settings() lru_cache
├── models.py                # Pydantic v2 data shapes (Run, Lead, Job, Approval, CRMOutboxOp, SentEmail, LeadHistoryEvent, ContactPolicy)
├── state_machine.py         # 16-state enum + ALLOWED_TRANSITIONS
├── state_store.py           # StateStore ABC + InMemoryStateStore
├── job_queue.py             # JobQueueProtocol + InMemoryJobQueue
├── supervisor.py            # Supervisor orchestrator (PLAN, STEP_TO_STATE)
├── worker.py                # asyncio worker loop + _build_default_worker() + build_in_process_supervisor()
├── tool_gateway.py          # ToolGateway + TOOL_POLICIES (5 tools) — UNWIRED in production
├── approval_gate.py         # Gate 1 / Gate 2 ApprovalGate
├── approval_store.py        # RealApprovalVerifier + InMemoryApprovalStore + compute_payload_hash
├── idempotency.py           # IdempotentToolExecutor
├── budget.py                # RunBudget
├── retry.py                 # retry exceptions
├── security_context.py      # SecurityContext, AuthContext, InMemoryAuditLogger, InMemoryRateLimiter
├── webhook.py               # verify_hmac (constant-time, raw bytes)
├── sent_email_store.py      # InMemorySentEmailStore — ONLY impl, always used in prod
├── lead_history_store.py    # InMemoryLeadHistoryStore — ONLY impl, always used in prod
├── services/
│   └── lead_intake.py       # LeadIntakeService (single intake entry point)
├── specialists/
│   └── __init__.py          # 6 specialists + build_specialists()
├── reconciler/
│   ├── crm_outbox.py        # CRMOutboxStore + CRMOutboxDrainer — UNWIRED
│   ├── email_send.py        # EmailSendReconciler — UNWIRED
│   └── approval_timeout.py  # ApprovalTimeoutReconciler — UNWIRED
├── guardrails/
│   ├── base.py              # GuardrailChain, OutputGuardrailChain
│   ├── input.py             # PIIRedactor, PromptInjectionHeuristic
│   └── output.py            # OutputSchemaValidator, GroundingChecker, LengthCap, RecipientValidator
├── mcp/
│   ├── base.py
│   ├── crm_server.py        # CRMMCPServer — never instantiated by worker
│   └── comms_server.py      # CommsMCPServer (suppression check at :119) — never instantiated by worker
├── observability/
│   ├── logging.py           # JSON logging
│   ├── tracing.py           # tracing stubs
│   └── metrics.py           # 8-metric dashboard
├── eval/
│   ├── runner.py            # eval harness
│   └── mocks.py
├── adapters/
│   ├── provider_factory.py  # create_*_provider() factories
│   ├── llm/{groq.py, mock.py}
│   ├── research/{exa.py, mock.py}
│   ├── email/{resend.py, mock.py}
│   ├── crm/{hubspot.py, mock.py}
│   ├── notifier/{mock.py}
│   └── db/
│       ├── connection.py    # backend detect (Postgres/SQLite/in-memory), _init_sqlite_schema
│       ├── schema.py        # SCHEMA_SQLITE / SCHEMA_POSTGRES
│       ├── lead_store.py    # InMemoryLeadStore + DatabaseLeadStore
│       ├── sqlite_state_store.py
│       ├── sqlite_job_queue.py
│       ├── sqlite_approval_store.py
│       └── sqlite_outbox_store.py
├── tools/
│   └── schemas.py           # MCP tool arg/result Pydantic schemas
└── api/
    ├── app.py               # create_app(), health endpoints, / redirect, /ui mount
    ├── auth.py              # get_current_user JWT verify, require_workspace (unused)
    ├── auth_google.py       # Google OAuth router — TO BE REMOVED
    └── routes/
        ├── leads.py         # GET/POST /leads, GET /leads/{id}, POST /demo/simulate
        ├── webhook.py       # POST /events/new_lead (HMAC)
        ├── tally_webhook.py # POST /events/tally (Tally)
        ├── runs.py          # GET /runs, /runs/{id}, /runs/{id}/timeline
        ├── approvals.py     # GET /approvals, /approvals/{id}, POST /approvals/{id}/decision
        ├── dashboard.py     # GET /dashboard (UNAUTHENTICATED)
        ├── metrics.py       # GET /metrics (UNAUTHENTICATED)
        ├── activity.py      # GET /activity, /activity/emails, /activity/history, /activity/leads/{id}
        └── integrations.py  # GET /integrations
```

---

## 3. The 16-state workflow state machine

File: `opsmesh/state_machine.py:21-57`. States:

1. `NEW` 2. `RESEARCHING` 3. `AWAITING_EVIDENCE_REVIEW` 4. `EVIDENCE_PROVIDED` 5. `QUALIFYING` 6. `DRAFTING` 7. `CRITIC` 8. `AWAITING_APPROVAL` 9. `SENDING` 10. `SEND_UNKNOWN` 11. `CRM_UPDATING` 12. `SENT` 13. `REJECTED` 14. `FAILED` 15. `STOPPED_BY_USER` 16. `APPROVAL_EXPIRED`

- `ALLOWED_TRANSITIONS`: `state_machine.py:86-154`
- `TERMINAL_STATES`: `:63` — `{SENT, REJECTED, FAILED, STOPPED_BY_USER, APPROVAL_EXPIRED}`
- `PAUSED_STATES`: `:73` — `{AWAITING_EVIDENCE_REVIEW, AWAITING_APPROVAL, SEND_UNKNOWN}`
- `transition()` / `can_transition()` / `IllegalTransition`: `:195` / `:216` / `:157`
- Gate 1 (`AWAITING_EVIDENCE_REVIEW`) allowed outs: `EVIDENCE_PROVIDED, DRAFTING, STOPPED_BY_USER, APPROVAL_EXPIRED` — **never authorizes SENDING** (correct).
- Gate 2 (`AWAITING_APPROVAL`) allowed outs: `SENDING, REJECTED, DRAFTING, APPROVAL_EXPIRED`.
- `SEND_UNKNOWN` allowed outs: `{SENT, FAILED}` — reconciler-only; supervisor never auto-transitions.

---

## 4. Provider adapters

| Provider | File | Real call site | Notes |
|---|---|---|---|
| Groq (LLM) | `adapters/llm/groq.py:62` | `groq.py:91` `GroqProvider.chat` POST `/openai/v1/chat/completions` | Default model `openai/gpt-oss-120b`; `MODEL_PRICING` at `groq.py:52` |
| Exa (research) | `adapters/research/exa.py:28` | `exa.py:51` POST `https://api.exa.ai/search` | |
| Resend (email) | `adapters/email/resend.py:52` | `resend.py:75` POST `https://api.resend.com/emails`; `get_status()` at `:126` GET `/emails/{id}` | `Idempotency-Key` header at `:88-89`; status map at `:42` |
| HubSpot (CRM) | `adapters/crm/hubspot.py:42` | `hubspot.py:147` POST `/crm/v3/objects/contacts`; `:105` PATCH `/contacts/{id}` | lead_id format `L_<digits>` |
| Tally webhook | `api/routes/tally_webhook.py:80` `normalize_tally_payload` + `:178` route | | HMAC **skippable by default** — see `CRITICAL_FINDINGS.md` P0-2 |
| Generic webhook | `api/routes/webhook.py:112` + `webhook.py:64` `verify_hmac` | | Constant-time HMAC on raw body; **enforced** (fail-closed) |
| Mocks | `adapters/{llm,research,email,crm,notifier}/mock.py` | | Used by CI / dev |
| Factory | `adapters/provider_factory.py:42` `create_llm_provider`, `:58` research, `:73` email, `:86` crm, `:99` notifier | | |

**Every real adapter is called directly by specialists, bypassing ToolGateway.** See `CRITICAL_FINDINGS.md` P0-1.

---

## 5. Database stores / persistence layer

| Store | In-memory impl | DB impl | Used in production? |
|---|---|---|---|
| Run state | `state_store.py:150` `InMemoryStateStore` | `adapters/db/sqlite_state_store.py:37` `SQLiteStateStore` | DB when configured |
| Job queue | `job_queue.py:109` `InMemoryJobQueue` | `adapters/db/sqlite_job_queue.py:37` `SQLiteJobQueue` (`BEGIN IMMEDIATE` claim) | DB when configured |
| Approvals | `approval_store.py:224` `InMemoryApprovalStore` | `adapters/db/sqlite_approval_store.py:43` `SQLiteApprovalStore` | DB when configured (**mixes sync `sqlite3` into async code** — see findings) |
| Leads | `adapters/db/lead_store.py:84` `InMemoryLeadStore` | `:140` `DatabaseLeadStore` | DB when configured |
| CRM outbox | `reconciler/crm_outbox.py:43` `CRMOutboxStore` | `adapters/db/sqlite_outbox_store.py:34` `SQLiteOutboxStore` | **NEVER wired** — dead code |
| Sent emails | `sent_email_store.py:51` `InMemorySentEmailStore` | **NONE** | **Always in-memory in prod** — vanishes on restart |
| Lead history | `lead_history_store.py:47` `InMemoryLeadHistoryStore` | **NONE** | **Always in-memory in prod** — vanishes on restart |
| Audit log | `security_context.py:174` `InMemoryAuditLogger` | `audit_events` table exists in schema but **nothing writes to it** | In-memory only |
| Rate limiter | `security_context.py:257` `InMemoryRateLimiter` | NONE | In-memory, per-process |
| Suppression | `services/lead_intake.py:144-149` `_suppression`, `_global_suppression` | `ContactPolicy` model exists (`models.py:455`) but never persisted | In-memory only |
| Webhook dedup | `services/lead_intake.py:129` `_dedup_index` | NONE | In-memory, 5-min window |

**Backend selection:** `adapters/db/connection.py:47-50` holds process-global `_db_path`, `_db_url`, `_db_backend="memory"`, `_pg_pool`. `configure_backend()` / `init_db()` / `reset_db_state()` mutate it. `app.py:151-158` and `worker.py:229-237` select SQLite/Postgres when `OPSMESH_DATABASE_URL` or `OPSMESH_DATABASE_PATH` is set, else in-memory. `production_mode=true` fails closed at startup if no DB is set (`app.py:90-95`).

**API and worker share the same DB** when both point at the same `OPSMESH_DATABASE_URL` (render.yaml wires both services to the same `opsmesh-db`). ✅ Verified.

---

## 6. Background worker + job queue

- `opsmesh/worker.py:46` `Worker` — polls `claim()` every `POLL_INTERVAL_SECONDS=1.0` (`worker.py:43`); calls `supervisor.run(JobForSupervisor)`; `complete()` or `dead_letter()` after `max_attempts`.
- `worker.py:174` `_build_default_worker()` — production factory (DB-backed stores, real providers, `create_approval_store()`).
- `worker.py:280` `build_in_process_supervisor()` — shared instances for the API's in-process worker mode (`OPSMESH_WORKER_IN_PROCESS`, default `"auto"`).
- `worker.py:361` `_main_async()` — `python -m opsmesh.worker` entrypoint; SIGTERM/SIGINT graceful shutdown.
- Job queue protocol: `job_queue.py:53`. SQLite claim: `BEGIN IMMEDIATE` (`sqlite_job_queue.py:74`). Lease 300s, max_attempts 4 (`job_queue.py:40-41`).
- **Reconcilers (`EmailSendReconciler`, `ApprovalTimeoutReconciler`, `CRMOutboxDrainer`) are never instantiated in `worker.py` or `app.py`** — only in tests. `SEND_UNKNOWN` runs never resolve; 24h approvals never expire; the outbox never drains.

---

## 7. Supervisor + specialist architecture

- `opsmesh/supervisor.py:114` `Supervisor` — owns workflow state. `run()` (`:147`) loads checkpoint, dynamic-loop `_next_step()` (`:211`), `_execute_step()` (`:243`) checkpoints before/after delegating, `_apply_result()` (`:347`) owns transitions. `_create_gate1_approval()` (`:488`) + `_create_gate2_approval()` (`:528`).
- **`_execute_step` calls `await specialist(run, budget=budget, ctx=None)` directly** (`supervisor.py:283-285`) — `ctx=None` means no `SecurityContext`, no `ToolGateway`.

Specialists (all in `opsmesh/specialists/__init__.py`):

| Specialist | Line | Role | External calls |
|---|---|---|---|
| `ResearchSpecialist` | `:113` | company + individual research | Exa `web_search` (`:162`, `:249`); Groq `chat` (`:188`, `:280`) |
| `QualifySpecialist` | `:344` | ICP qualification, writes `lead.score` | Groq `chat` (`:390`) |
| `DraftSpecialist` | `:472` | email draft (company vs individual prompts) | Groq `chat` (`:556`) |
| `CriticSpecialist` | `:621` | review draft (lenient by default) | Groq `chat` (`:644`) |
| `SendingSpecialist` | `:690` | Resend send; honors `email_kill_switch`; **synthesizes fake `approval_id=f"ap_{run_hex}"`** (`:763`) | Resend `send` (`:773`) |
| `CRMUpdatingSpecialist` | `:792` | **calls `crm_provider.create_lead()` directly, no outbox** (`:807`) | HubSpot `create_lead` |
| `build_specialists()` | `:825` | factory | |

Critic retry: `MAX_DRAFT_ATTEMPTS=2` (`supervisor.py:79`); unknown verdict → FAILED (fail-closed, `:417`).

---

## 8. ToolGateway + guardrails

- **Definition:** `opsmesh/tool_gateway.py:265` `ToolGateway` — 7 checks inside `execute()` (`:301-407`): tool policy, scope, approval+payload_hash via `RealApprovalVerifier.verify_and_consume`, allowlist args, rate limit, idempotency via `IdempotentToolExecutor`, audit log.
- **`TOOL_POLICIES`** for 5 tools at `tool_gateway.py:113`: `lookup_lead`, `update_lead_status`, `create_lead`, `send_email` (NOTIFY, requires Gate 2), `send_notification`.
- **Instantiation sites:** ONLY in `tests/unit/test_tool_gateway.py:113, 649, 835, 864`. **Zero production wiring.** See `CRITICAL_FINDINGS.md` P0-1.
- Guardrails: `guardrails/base.py:115` `GuardrailChain`, `:158` `OutputGuardrailChain`. Input (`guardrails/input.py:38` `PIIRedactor`, `:154` `PromptInjectionHeuristic`); Output (`guardrails/output.py:31` `OutputSchemaValidator`, `:93` `GroundingChecker`, `:213` `LengthCap`, `:270` `RecipientValidator`). **Not invoked by specialists in production.**

---

## 9. CRM outbox + reconciliation (DEAD CODE in production)

- Models: `models.py:479-509` `CRMOutboxOperation`, `CRMOutboxOp`, `CRMOutboxStatus`.
- Stores: `reconciler/crm_outbox.py:43` in-memory; `adapters/db/sqlite_outbox_store.py:34` durable.
- Drainer: `reconciler/crm_outbox.py:117` `CRMOutboxDrainer` — `run_once()`, backoff `[30,60,120,240,480]`s, max attempts → DEAD_LETTER.
- Email reconciler: `reconciler/email_send.py:42` `EmailSendReconciler` — resolves `SEND_UNKNOWN`; `MAX_RECONCILIATION_ATTEMPTS=5`; **pokes `InMemoryStateStore._runs` directly (`:80-81`) — silently no-ops with `SQLiteStateStore`**.
- Approval timeout: `reconciler/approval_timeout.py:37` `ApprovalTimeoutReconciler` — expires pending approvals → `APPROVAL_EXPIRED`.
- **None of these are constructed in `app.py` or `worker.py`.** The outbox table is never populated. `CRMUpdatingSpecialist` calls the CRM directly.

---

## 10. API routes (FastAPI routers)

Registered in `opsmesh/api/app.py:334-343`. Health: `/health`, `/ready`, `/api/v1/health` (`app.py`).

| Route file | Endpoints | Auth? | Workspace binding |
|---|---|---|---|
| `api/routes/leads.py` | `GET /api/v1/leads` (paginated), `POST /api/v1/leads` (manual), `GET /api/v1/leads/{id}`, `POST /api/v1/demo/simulate` | only `list_leads` guarded; **create/get/simulate UNAUTHENTICATED** | hardcoded `_workspace_id="default"` (`:42-43`) |
| `api/routes/webhook.py` | `POST /api/v1/events/new_lead` (HMAC raw body) | HMAC | hardcoded `"default"` (`:147`) |
| `api/routes/tally_webhook.py` | `POST /api/v1/events/tally` | HMAC **skippable** (`:196` `if _tally_secret:`) | hardcoded `"default"` (`:222`) |
| `api/routes/runs.py` | `GET /api/v1/runs`, `/runs/{id}`, `/runs/{id}/timeline` | guarded | `user["workspace_id"]` |
| `api/routes/approvals.py` | `GET /api/v1/approvals`, `/{id}`, `POST /{id}/decision` | guarded | inline check `:155` |
| `api/routes/dashboard.py` | `GET /api/v1/dashboard` | **UNAUTHENTICATED** | `_workspace_id="default"` (`:35`) |
| `api/routes/metrics.py` | `GET /api/v1/metrics` | **UNAUTHENTICATED** | `_workspace_id="default"` (`:26`) |
| `api/routes/activity.py` | `GET /api/v1/activity`, `/activity/emails`, `/activity/history`, `/activity/leads/{lead_id}` | guarded | `user["workspace_id"]` (but `InMemorySentEmailStore` is process-global — cross-workspace read risk) |
| `api/routes/integrations.py` | `GET /api/v1/integrations` | guarded | n/a |
| `api/auth_google.py` | `GET /api/v1/auth/google/client-id`, `POST /api/v1/auth/google` | — | **TO BE REMOVED** |

Module-level singletons (`_store`, `_intake_service`, `_workspace_id`, etc.) in every route file mean `create_app()` is **not safe to call twice in one process** — the second app overwrites the first's singletons.

---

## 11. Frontend pages

All under `opsmesh/static/`, served at `/ui` (`app.py:396-402`). No Jinja; static HTML booting a vanilla-JS SPA via inline `<script>`.

| File | Purpose |
|---|---|
| `static/index.html:5` | Meta-refresh redirect to `/ui/login.html` |
| `static/login.html` | Google OAuth or dev-mode entry — **TO BE REMOVED** |
| `static/dashboard.html` | Overview: 8 stat tiles + recent runs + pending approvals + DLQ |
| `static/leads.html` | Leads list + create form (**no detail view; rows not clickable**) |
| `static/runs.html` | Runs list (filters) + run detail (budget, draft, research, qualification, critic, timeline) |
| `static/approvals.html` | Approvals list + detail (Gate 1 evidence / Gate 2 email) with decision buttons + draft preview |
| `static/activity.html` | Two tabs: Sent Emails + History (siloed, not unified) |
| `static/integrations.html` | Server health + provider cards (abstract roles, not vendor names; **no Tally, no Worker**) |
| `static/css/app.css` | ~685 lines, dark theme, single breakpoint at 768px |
| `static/js/app.js` | Global `OPSMESH` object: `api`, `auth`, `ui`; localStorage token storage (`opsmesh_token`, `opsmesh_user`) |
| `static/js/shell.js` | Sidebar shell + `NAV_ITEMS` (6 items — **no Settings/Operations**) + logout |

README references `/ui/settings.html` (`README.md:159`) — **that file does not exist**.

Required nav coverage: Overview ✅, Leads ⚠️ (list only), Runs ✅, Approvals ✅, Activity ⚠️ (split), Integrations ⚠️ (no Tally/Worker), Settings ❌ (missing).

---

## 12. Tests

- 33 test files: `tests/unit/` (31), `tests/integration/` (2: `test_api.py`, `test_real_providers.py`), `tests/failure/test_failure_injection.py`. `tests/conftest.py`, `tests/fixtures/llm/`.
- `def test_` definitions: **743** (function count). README claims **"739 passed"**.
- **In a clean environment, `pytest --collect-only` collects only 530 tests with 8 collection errors** (missing `aiosqlite`/`sqlalchemy`/`alembic` deps). See `BASELINE_TEST_RESULTS.md` for the full re-run after dep install.
- Integration tests require real API keys (`tests/integration/test_real_providers.py`).
- `pyproject.toml` markers: `unit`, `integration`, `security`, `failure`, `eval`. `addopts = "-ra --strict-markers"`.
- Makefile `test` target uses `--cov-fail-under=80`.

---

## 13. Deployment files

- `render.yaml` — Render blueprint: `opsmesh-db` Postgres (free), `opsmesh-api` web service, `opsmesh-worker` background worker. Sets `OPSMESH_PRODUCTION_MODE=true`, `OPSMESH_EMAIL_KILL_SWITCH=false`, providers as `sync: false`. **Worker missing `OPSMESH_PRODUCTION_MODE`, `OPSMESH_CORS_ALLOWED_ORIGINS`, `OPSMESH_TALLY_WEBHOOK_SECRET`, `OPSMESH_GOOGLE_OAUTH_CLIENT_ID`.**
- `Dockerfile` — `python:3.12-slim`, non-root `opsmesh` user, `uvicorn opsmesh.api.app:create_app --factory`, `HEALTHCHECK /health`.
- `Procfile` — `web:` uvicorn, `worker:` `python -m opsmesh.worker`.
- `alembic.ini` — script_location `alembic`, default `sqlite:///./opsmesh.db` (overridden by `alembic/env.py`).
- `alembic/env.py` — reads `OPSMESH_DATABASE_PATH`, `render_as_batch=True` for SQLite.
- `Makefile` — `demo`, `test` (pytest `--cov-fail-under=80`), `eval`, `lint` (ruff + mypy --strict), `db-reset/seed/migrate`.
- `.env.example`, `requirements.txt`, `pyproject.toml`, `.dockerignore`.

See `DEPLOYMENT_BASELINE.md` for env-var drift and `DEPENDENCY_MAP.md` for version drift.

---

## 14. Functionality that MUST remain operational

Per the mandate's non-negotiable preservation rules, the following are explicitly protected and must survive every phase of the transformation:

1. The 16-state machine and its transition rules (`state_machine.py`).
2. Gate 1 (`AWAITING_EVIDENCE_REVIEW`) and Gate 2 (`AWAITING_APPROVAL`) — pause/resume/decide.
3. The CRM outbox + reconciliation architecture (currently dead code — **wiring it in is required, not removing it**).
4. Idempotency (`IdempotentToolExecutor`, Resend `Idempotency-Key` header).
5. Budget controls (`RunBudget`, `MAX_COST_USD`, `MAX_TOKENS_*`, `MAX_RUN_SECONDS`).
6. Provider interfaces (`create_*_provider` factory + the mock/real split).
7. Webhook HMAC verification (generic webhook — fail-closed).
8. The two approval gates' payload-hash binding design (`compute_payload_hash`, `TOOL_PROTECTED_FIELDS`).
9. The lead-intake → job-enqueue → research → qualify → draft → critic → approve → send → CRM flow.
10. The API + worker shared-database contract (render.yaml wires both to `opsmesh-db`).

---

## 15. Cross-cutting structural observations

- **Module-level mutable singletons in every route file** (`_store`, `_intake_service`, `_workspace_id`, etc.) make `create_app()` non-idempotent within a process and couple tests to global state.
- **The process-global DB backend** (`adapters/db/connection.py:47-50`) means every test that touches the DB must `reset_db_state()` or it leaks into the next test.
- **`InMemorySentEmailStore` and `InMemoryLeadHistoryStore` are the only implementations** and are always instantiated in `create_app()` (`app.py:175-176`). The Activity/Outbox tab and the agent's memory are wiped on every redeploy.
- **`SQLiteApprovalStore` and `SQLiteOutboxStore` mix synchronous `sqlite3.connect()` calls into async code** — blocks the event loop on every call (DoS vector under load, correctness hazard with WAL).
- **`EmailSendReconciler` pokes `InMemoryStateStore._runs` directly** — silently no-ops with `SQLiteStateStore`.
- **Hardcoded sender `from_="bekalu@opsmesh.dev"` in Gate 2 approval payload** (`supervisor.py:550`) diverges from the actual send identity (`specialists/__init__.py:709` reads `workspace_sender_identity`). If ToolGateway were wired in, the payload-hash binding would reject legitimately-approved sends.
