# OPSMESH Deployment Baseline

**Audit date:** 2026-11-12
**Repo:** `/home/z/my-project/opsmesh/` (commit `4ec6cdf`)

This document records the current Render deployment configuration, the env-var drift between `render.yaml`, `.env.example`, the `Settings` class, and actual code usage, plus the persistent-storage, worker, and health-check posture. It is the basis for the deployment changes (and rollback plan) in `STAGE_A_PLAN.md`.

---

## 1. Render blueprint (`render.yaml`)

**Services defined:**

| Service | Type | Runtime | Build | Start | Health |
|---|---|---|---|---|---|
| `opsmesh-db` | database (Postgres) | — | — | — | plan: **free**, `ipAllowList: []` |
| `opsmesh-api` | web | python | `pip install -r requirements.txt` | `uvicorn opsmesh.api.app:create_app --factory --host 0.0.0.0 --port $PORT` | `GET /health`, autoDeploy: true |
| `opsmesh-worker` | worker | python | `pip install -r requirements.txt` | `python -m opsmesh.worker` | (workers have no HTTP health check), autoDeploy: true |

**Database:** Render Postgres free tier. Both API and worker pull the connection string via:
```yaml
- key: OPSMESH_DATABASE_URL
  fromDatabase:
    name: opsmesh-db
    property: connectionString
```
**API and worker share the same database and schema.** ✅ Verified — both services bind to `opsmesh-db`.

**API env vars set in `render.yaml`:**

| Key | Value | Notes |
|---|---|---|
| `OPSMESH_DATABASE_URL` | from `opsmesh-db` | ✅ |
| `OPSMESH_LLM_PROVIDER` | `groq` | ✅ |
| `OPSMESH_LLM_API_KEY` | `sync: false` (secret) | ✅ |
| `OPSMESH_LLM_DEFAULT_MODEL` | `openai/gpt-oss-120b` | ✅ |
| `OPSMESH_RESEARCH_PROVIDER` | `exa` | ✅ |
| `OPSMESH_EXA_API_KEY` | `sync: false` | ✅ |
| `OPSMESH_EMAIL_PROVIDER` | `resend` | ⚠️ demo risk — P0-3 |
| `OPSMESH_EMAIL_API_KEY` | `sync: false` | ✅ |
| `OPSMESH_EMAIL_KILL_SWITCH` | `"false"` | ⚠️ **P0** — flips the only email safety OFF |
| `OPSMESH_CRM_PROVIDER` | `hubspot` | ⚠️ demo risk — P0-4 |
| `OPSMESH_CRM_API_KEY` | `sync: false` | ✅ |
| `OPSMESH_WEBHOOK_SECRET` | `sync: false` | ✅ |
| `OPSMESH_SESSION_SECRET` | `sync: false` | will be removed with OAuth |
| `OPSMESH_GOOGLE_OAUTH_CLIENT_ID` | `sync: false` | will be removed with OAuth |
| `OPSMESH_CORS_ALLOWED_ORIGINS` | `"*"` | ⚠️ **P1** — wildcard CORS |
| `OPSMESH_PRODUCTION_MODE` | `"true"` | ✅ (API only) |

**Worker env vars set in `render.yaml`:**

The worker block repeats `OPSMESH_DATABASE_URL`, `OPSMESH_LLM_*`, `OPSMESH_RESEARCH_*`, `OPSMESH_EMAIL_*`, `OPSMESH_CRM_*`, `OPSMESH_WEBHOOK_SECRET` — but is **missing**:

| Missing from worker | Impact |
|---|---|
| `OPSMESH_PRODUCTION_MODE` | Worker does not enforce prod-mode startup checks (the `app.py:90-95` fail-closed on missing webhook secret / `"change-me"` only fires in the API process). |
| `OPSMESH_CORS_ALLOWED_ORIGINS` | Cosmetic for a worker (no HTTP), but drift from API config. |
| `OPSMESH_TALLY_WEBHOOK_SECRET` | Missing from both services — Tally webhook accepts unsigned POSTs (P0-2). |
| `OPSMESH_GOOGLE_OAUTH_CLIENT_ID` | Inconsistent with API; will be removed with OAuth anyway. |
| `OPSMESH_WORKSPACE_OWNER_EMAIL`, `OPSMESH_WORKSPACE_SENDER_IDENTITY` | Missing from both — defaults to `owner@example.com` and `Bekalu <bekalu@opsmesh.dev>` (the latter is the email `From:` header — P1). |
| `OPSMESH_OBS_BACKEND`, `OPSMESH_OTEL_EXPORTER_OTLP_ENDPOINT` | Observability off in prod. |

---

## 2. Env-var drift matrix

Comparing `render.yaml`, `.env.example`, `Settings` class (`opsmesh/config.py`), and code that reads `os.environ.get(...)` directly:

| Var | render.yaml | .env.example | Settings field | Code reads | Severity |
|---|---|---|---|---|---|
| `OPSMESH_DATABASE_URL` | ✅ both services | ✅ (commented) | ✅ `database_url` | `connection.py` | — |
| `OPSMESH_DATABASE_PATH` | ❌ | ✅ | ✅ `database_path` | `connection.py`, `alembic/env.py` | P3 |
| `OPSMESH_PRODUCTION_MODE` | ✅ API, ❌ worker | ✅ | ✅ `production_mode` | `app.py:90` | P2 |
| `OPSMESH_LLM_PROVIDER` | ✅ | ✅ | ✅ `llm_provider` | `provider_factory.py` | — |
| `OPSMESH_LLM_API_KEY` | ✅ | ✅ | ✅ `llm_api_key` | `provider_factory.py` | — |
| `OPSMESH_LLM_DEFAULT_MODEL` | ✅ | ✅ | ✅ `llm_default_model` | `groq.py` | — |
| `OPSMESH_RESEARCH_PROVIDER` / `_API_KEY` | ✅ | ✅ | ✅ | `provider_factory.py` | — |
| `OPSMESH_EMAIL_PROVIDER` / `_API_KEY` | ✅ | ✅ | ✅ | `provider_factory.py` | — |
| `OPSMESH_EMAIL_KILL_SWITCH` | `"false"` | `true` | ✅ `email_kill_switch` (default `True`) | `specialists/__init__.py:744` | **P0** — render flips the safe default OFF |
| `OPSMESH_EMAIL_FROM` | ❌ | ✅ (commented, line 34) | ❌ **no such field** | — | **P2** — documented var does nothing |
| `OPSMESH_WORKSPACE_SENDER_IDENTITY` | ❌ | ✅ (line 75) | ✅ `workspace_sender_identity` (default `Bekalu <bekalu@opsmesh.dev>`) | `specialists/__init__.py:709`, `supervisor.py:550` | **P1** — real `From:` header; unverified domain (`opsmesh.dev`) triggers Resend 403 |
| `OPSMESH_WORKSPACE_OWNER_EMAIL` | ❌ | ✅ (line 74) | ✅ `workspace_owner_email` | seed | P2 |
| `OPSMESH_CRM_PROVIDER` / `_API_KEY` | ✅ | ✅ | ✅ | `provider_factory.py` | — |
| `OPSMESH_WEBHOOK_SECRET` | ✅ | ✅ | ✅ `webhook_secret` | `webhook.py` via `app.py` configure | — |
| `OPSMESH_TALLY_WEBHOOK_SECRET` | ❌ | ✅ (line 40) | ✅ `tally_webhook_secret` (default `""`) | `tally_webhook.py` | **P0** — accepts unsigned POSTs |
| `OPSMESH_SESSION_SECRET` | ✅ API | ✅ (line 44) | ✅ `session_secret` | `auth_google.py` | will be removed with OAuth |
| `OPSMESH_GOOGLE_OAUTH_CLIENT_ID` | ✅ API | ✅ (line 50) | ✅ `google_oauth_client_id` | `auth_google.py`, `app.py:367` | will be removed with OAuth |
| `OPSMESH_GOOGLE_OAUTH_CLIENT_SECRET` | ❌ | ✅ (line 51) | ✅ `google_oauth_client_secret` | **never read** | P3 — dead config |
| `OPSMESH_CORS_ALLOWED_ORIGINS` | `"*"` | ✅ | ❌ **no Settings field** | `app.py:295` `os.environ.get` direct | **P1** — wildcard + `allow_credentials=True` (`app.py:304`) |
| `OPSMESH_OBS_BACKEND` | ❌ | ✅ (line 59) | ✅ `obs_backend` | `observability/logging.py` | P3 |
| `OPSMESH_OTEL_EXPORTER_OTLP_ENDPOINT` | ❌ | ✅ (line 60) | ✅ `otel_exporter_otlp_endpoint` | tracing stub | P3 |
| `OPSMESH_WORKER_IN_PROCESS` | ❌ | ❌ | ✅ `worker_in_process` (default `"auto"`) | `app.py:225` | P3 — undocumented |
| `WEBHOOK_SECRET` (unprefixed) | ❌ | ❌ | ❌ | `webhook.py:95` `os.environ.get("WEBHOOK_SECRET","")` fallback | **P2** — always returns `""`; the configured name is `OPSMESH_WEBHOOK_SECRET`. The route handler passes `_secret` explicitly so it works, but anyone calling `verify_hmac()` without the secret arg gets a silent fail-closed. |
| `OPSMESH_HUBSPOT_API_KEY` | ❌ | ❌ | ❌ | — (only mentioned in `hubspot.py:12` docstring) | P3 — doc references nonexistent var; real name is `OPSMESH_CRM_API_KEY` |
| Engineering contract (`OPSMESH_CONCURRENCY_LOCAL`, `OPSMESH_MAX_STEPS`, `OPSMESH_MAX_TOOL_CALLS`, `OPSMESH_MAX_TOKENS_*`, `OPSMESH_MAX_COST_USD`, `OPSMESH_MAX_RUN_SECONDS`, `OPSMESH_APPROVAL_TIMEOUT_HOURS`, `OPSMESH_JOB_LEASE_SECONDS`) | ❌ | ✅ (lines 63-72) | ✅ | various | P3 — defaults in code are used |

---

## 3. Persistent storage

| Data | Persisted? | Where | Survives redeploy? |
|---|---|---|---|
| Runs / state | ✅ when DB configured | `runs` table (Postgres on Render) | ✅ |
| Jobs / queue | ✅ | `jobs` table | ✅ |
| Approvals | ✅ | `approvals` table | ✅ |
| Idempotency records | ✅ | `idempotency` table | ✅ |
| Leads | ✅ | `leads` table | ✅ |
| CRM outbox | ✅ schema exists | `crm_outbox` table | ✅ (but **never written to** — dead) |
| Audit events | ✅ schema exists | `audit_events` table | ✅ (but **never written to** — dead) |
| Evidence items | ✅ schema exists | `evidence_items` table | ✅ (but unwired) |
| **Sent emails** | ❌ **in-memory only** | `InMemorySentEmailStore` (`app.py:175`) | ❌ **lost on every redeploy** |
| **Lead history events** | ❌ **in-memory only** | `InMemoryLeadHistoryStore` (`app.py:176`) | ❌ **lost on every redeploy** |
| **Suppression list** | ❌ in-memory | `LeadIntakeService._suppression` | ❌ |
| **Webhook dedup index** | ❌ in-memory (5-min window) | `LeadIntakeService._dedup_index` | ❌ (acceptable per docstring, but cross-process workers each have their own) |
| **Audit log (gateway)** | ❌ in-memory | `InMemoryAuditLogger` (default in `ToolGateway.__init__`) | ❌ (and gateway is unwired anyway) |
| **Rate limiter state** | ❌ in-memory, per-process | `InMemoryRateLimiter` | ❌ multi-worker each have own bucket |

**Render free-tier disk:** Render free web services have an ephemeral filesystem — anything not in the Postgres database is lost on every redeploy or sleep cycle. The in-memory stores above are therefore guaranteed to vanish in production.

---

## 4. Worker configuration

- `render.yaml` worker start: `python -m opsmesh.worker`
- `worker.py:361` `_main_async()` — SIGTERM/SIGINT graceful shutdown; polls `claim()` every 1.0s.
- Concurrency: single asyncio loop, one job at a time per worker process. Render free worker = 1 instance.
- Lease: `OPSMESH_JOB_LEASE_SECONDS=300` (5 min). If a worker dies mid-job, the job is stuck for 5 min before another worker can reclaim it (no reconciler enforces this — `ApprovalTimeoutReconciler` is unwired, and there is no separate lease-expiry sweeper).
- `max_attempts=4` (`job_queue.py:41`) → dead-letter queue. DLQ is queryable via `GET /api/v1/dashboard` but has **no recovery UI** (no retry-from-DLQ button).

**Worker restart recovery:** on Render, if the worker restarts mid-run, the run is left in its last-checkpointed state. The supervisor's `run()` loads the checkpoint (`supervisor.py:147`) and resumes from `_next_step`. **This works correctly** — the checkpoint-before-and-after-each-step design (`supervisor.py:243`) is sound. The gap is that **`SEND_UNKNOWN` runs never resume** because the `EmailSendReconciler` is unwired.

**Free-tier sleep behavior:** Render free web services sleep after 15 min of inactivity. The worker is a separate service and does not sleep, but if the database is also free-tier, it has its own connection limits. No action required for the demo scope, but document it.

---

## 5. Health checks

`render.yaml` API healthCheckPath: `/health`.

Three health endpoints exist (`app.py`):
- `GET /health` — liveness (returns 200 if the process is up).
- `GET /ready` — readiness.
- `GET /api/v1/health` — full status including `auth.mode`, `google_oauth_configured`, `session_secret_configured`, `jwt_secret_configured`, store backend, queue backend, production mode.

**Gap:** the mandate requires "health checks distinguish liveness from readiness." Currently `/health` and `/ready` are not differentiated — both return process-up status without checking DB connectivity or worker heartbeat. There is **no worker health endpoint** at all (workers have no HTTP listener).

**Fix (Stage B):** make `/ready` actually probe the database (`SELECT 1`) and return 503 if it fails; keep `/health` as a trivial liveness probe. Add a worker heartbeat row (last `claim()` timestamp) to a `worker_heartbeat` table or to `audit_events`, surfaced on `/api/v1/integrations`.

---

## 6. CORS

`app.py:301-307`:
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("OPSMESH_CORS_ALLOWED_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

`render.yaml` sets `OPSMESH_CORS_ALLOWED_ORIGINS="*"`. Combined with `allow_credentials=True`, this is a misconfiguration: browsers reject `Access-Control-Allow-Origin: *` with credentials, but the intent is clearly "allow all origins" and any non-credentialed path is wide open. After OAuth removal, the credential path is gone, but the wildcard still allows any origin to hit the (now unauthenticated) `/dashboard`, `/metrics`, `/leads`, `/demo/simulate` endpoints.

**Fix (Stage B):** set `OPSMESH_CORS_ALLOWED_ORIGINS` to the actual frontend origin in `render.yaml` (the API serves the frontend itself at `/ui`, so CORS can be restricted to the API's own origin or disabled entirely for same-origin serving).

---

## 7. Migration execution

`render.yaml` does **not** run Alembic migrations as part of the build or start command. The API start is:
```
uvicorn opsmesh.api.app:create_app --factory --host 0.0.0.0 --port $PORT
```

Schema is created at runtime by `adapters/db/connection.py:_init_sqlite_schema` (`SCHEMA_SQLITE`/`SCHEMA_POSTGRES`) when the app boots — `CREATE TABLE IF NOT EXISTS ...` for every table. Alembic's single migration (`001_initial_schema.py`) exists but is **never invoked in the deploy**.

**Drift:** the runtime DDL (`connection.py` schema) and the Alembic migration (`alembic/versions/001_initial_schema.py`) are two independent sources of truth. They are not guaranteed to match. The audit found:
- Runtime schema adds `workspace_members` (`adapters/db/schema.py:185`).
- Runtime schema creates `sent_emails` and `lead_history_events` tables (in `SCHEMA_SQLITE`/`SCHEMA_POSTGRES`) — **but no Python store reads from them** (the in-memory stores are used instead).
- Alembic migration creates 8 tables but not `workspace_members`, `sent_emails`, or `lead_history_events`.

**Fix (Stage B):** pick Alembic as the single source of truth. Add a `render.yaml` preDeployCommand: `alembic upgrade head`. Make `connection.py` skip `CREATE TABLE IF NOT EXISTS` in production (or keep it as a dev-only convenience). Add a migration test that verifies `alembic upgrade head` produces a schema matching the runtime DDL.

---

## 8. Secrets

`render.yaml` marks all API keys as `sync: false` (entered in the Render dashboard, not in the repo). ✅ Good.

`.gitignore` excludes `.env`. ✅ Good.

No secrets are committed in code (the `change-me` defaults in `.env.example` are placeholders, not real keys). ✅ Good.

**Gaps:**
- The global exception handler (`app.py:313-331`) sanitizes responses, but per-route `HTTPException` details in `auth_google.py:161, 172, 177` and `auth.py:161` echo `str(e)[:200]` from PyJWT/httpx — potential credential leakage in error responses. (These go away with OAuth removal.)
- `last_error` is stored on the run (`supervisor.py:333-340`, truncated to 200 chars) and returned verbatim via `/api/v1/runs` and `/runs/{id}/timeline` — could echo provider error bodies. Low risk but worth scrubbing in Stage B.
- The Integrations endpoint returns `api_key_configured: bool` only — does not leak key values. ✅ Good.

---

## 9. Free-tier limitations

| Constraint | Impact on OPSMESH |
|---|---|
| Postgres free: 90 days, then deleted | Demo data wiped after 90 days; acceptable for demo scope |
| Web service free: sleeps after 15 min inactivity | First request after sleep has ~30s cold start; the worker (separate service) keeps the queue drained |
| Worker free: 1 instance | Single-threaded job processing; fine for demo volume |
| No persistent disk | All in-memory stores (sent emails, history, suppression) vanish on redeploy — see §3 |

**No deployment plan change is required for the demo scope.** The free tier supports the improved system as long as the in-memory stores are replaced with DB-backed ones (Stage B Phase 4).

---

## 10. Deployment changes proposed for Stage B

| Change | File | Why | Rollback |
|---|---|---|---|
| Add `OPSMESH_ACCESS_MODE=single_user_demo` | `render.yaml`, `config.py` | Implements the mandate's auth-removal mode | Set `OPSMESH_ACCESS_MODE=multi_user` (future) — the OAuth code stays in tree behind a flag |
| Set `OPSMESH_EMAIL_KILL_SWITCH=true` in `render.yaml` until demo verified | `render.yaml` | Stop the demo from sending real emails until sender identity is configured | Flip back to `"false"` |
| Set `OPSMESH_CRM_PROVIDER=mock` in `render.yaml` for demo | `render.yaml` | Stop the demo from mutating a real HubSpot | Flip back to `hubspot` |
| Add `OPSMESH_EMAIL_TEST_RECIPIENT=<your email>` | `render.yaml`, `config.py` | Restrict Resend sends to one address in demo mode | Remove the var to allow any recipient (future prod) |
| Set `OPSMESH_WORKSPACE_SENDER_IDENTITY=OpsMesh <onboarding@resend.dev>` | `render.yaml` | Use Resend's pre-verified test sender — fixes the 403 in the user's screenshot | Set to your own verified domain (future prod) |
| Add `OPSMESH_TALLY_WEBHOOK_SECRET` to both services | `render.yaml` | Close the unsigned-Tally-POST hole (P0-2) | Remove to revert (not recommended) |
| Remove `OPSMESH_GOOGLE_OAUTH_*`, `OPSMESH_SESSION_SECRET` from `render.yaml` | `render.yaml` | OAuth removal | Re-add (OAuth code stays in tree) |
| Tighten `OPSMESH_CORS_ALLOWED_ORIGINS` to the API origin | `render.yaml` | Close wildcard CORS | Set back to `"*"` (not recommended) |
| Add `preDeployCommand: alembic upgrade head` | `render.yaml` | Make Alembic the source of truth for schema | Remove the preDeployCommand; runtime `CREATE TABLE IF NOT EXISTS` still works |
| Add `OPSMESH_PRODUCTION_MODE=true` to worker env | `render.yaml` | Worker enforces prod-mode checks | Remove (not recommended) |

**All changes are env-var edits + a one-line preDeployCommand. No infrastructure plan change, no paid service introduced.** Rollback is reverting the env vars. This respects the mandate: "Do not silently change infrastructure plans or introduce paid services."

---

## 11. Deployment health summary

| Question | Answer |
|---|---|
| Do API and worker share the same database? | **Yes** ✅ |
| Are migrations run on deploy? | **No** — runtime `CREATE TABLE IF NOT EXISTS` is used instead |
| Is persistent storage used for all durable data? | **No** — sent emails, history, suppression, audit, rate-limit are in-memory |
| Are health checks differentiated (liveness vs readiness)? | **No** — `/health` and `/ready` are both trivial |
| Is the worker observable? | **No** — no worker health endpoint, no heartbeat |
| Is CORS configured safely? | **No** — wildcard + credentials |
| Are secrets committed? | **No** ✅ |
| Does the deploy block on a safe demo config by default? | **No** — `EMAIL_KILL_SWITCH=false`, `CRM_PROVIDER=hubspot`, `TALLY_WEBHOOK_SECRET` unset |
