# OPSMESH Stage A — Audit & Implementation Plan

**Audit date:** 2026-11-12
**Repo:** `/home/z/my-project/opsmesh/` (branch `main`, commit `4ec6cdf`)
**Stage:** A (audit + planning only — **no application code modified**)
**Status:** Awaiting explicit user approval before Stage B implementation begins.

This is the single synthesis document for Stage A. It consolidates the five companion audit documents (`BASELINE_INVENTORY.md`, `BASELINE_TEST_RESULTS.md`, `CRITICAL_FINDINGS.md`, `DEPENDENCY_MAP.md`, `DEPLOYMENT_BASELINE.md`) into a target architecture, a phased implementation plan, file-by-file expected changes, a migration strategy, a regression test plan, a deployment/rollback plan, complexity/risk estimates, a phase dependency graph, and the explicitly deferred features. Per the mandate's Stage A protocol, **no application code has been changed** to produce this document.

---

## Table of contents

1. [Baseline summary](#1-baseline-summary)
2. [Confirmed critical defects (P0)](#2-confirmed-critical-defects-p0)
3. [Suspected risks](#3-suspected-risks)
4. [Security assessment](#4-security-assessment)
5. [Architecture assessment](#5-architecture-assessment)
6. [Frontend assessment](#6-frontend-assessment)
7. [Database and deployment assessment](#7-database-and-deployment-assessment)
8. [Research findings from comparable products](#8-research-findings-from-comparable-products)
9. [Recommended target architecture](#9-recommended-target-architecture)
10. [Detailed implementation phases](#10-detailed-implementation-phases)
11. [File-by-file expected changes](#11-file-by-file-expected-changes)
12. [Dependency changes](#12-dependency-changes)
13. [Database migration strategy](#13-database-migration-strategy)
14. [Regression test plan](#14-regression-test-plan)
15. [Deployment and rollback plan](#15-deployment-and-rollback-plan)
16. [Estimated complexity and risk per phase](#16-estimated-complexity-and-risk-per-phase)
17. [Features deliberately deferred](#17-features-deliberately-deferred)
18. [Phase dependency graph](#18-phase-dependency-graph)
19. [What I will NOT do in Stage B without re-approval](#19-what-i-will-not-do-in-stage-b-without-re-approval)

---

## 1. Baseline summary

| Question | Answer | Source |
|---|---|---|
| Is the existing system a working baseline? | **Yes.** 739 unit tests pass in 10.94s once declared deps are installed. | `BASELINE_TEST_RESULTS.md` |
| Is the README's "739 passed" claim true? | **Yes — verified.** | `BASELINE_TEST_RESULTS.md` §3 |
| Does `pip install -r requirements.txt` succeed cleanly? | **No** — `sqlalchemy==2.1.8` is an invalid pin. | `DEPENDENCY_MAP.md` §3 |
| Are all external side effects governed by ToolGateway? | **No** — all 4 (Groq, Exa, Resend, HubSpot) bypass it. | `CRITICAL_FINDINGS.md` P0-1 |
| Is the deploy safe to demo publicly? | **No** — 8 P0 defects block it. | `CRITICAL_FINDINGS.md` §P0 |
| Is data durable across restarts? | **Partially** — runs/jobs/approvals/leads are DB-backed; sent emails, lead history, suppression, audit, rate-limit are in-memory. | `DEPLOYMENT_BASELINE.md` §3 |
| Is Google OAuth removable cleanly? | **Yes** — concentrated in 3 backend files + 2 frontend files; `get_current_user` is the only dependency to preserve. | `CRITICAL_FINDINGS.md` §1 (security audit) |

**One-line summary:** OPSMESH is a structurally sound 16-state agent workflow with a green test suite, but its policy layer (ToolGateway), reconciliation layer, persistence layer, and security boundary are all **defined but unwired** — the system ships the architecture without enforcing it. Stage B's job is to wire the existing architecture in, not to rewrite it.

---

## 2. Confirmed critical defects (P0)

Eight P0 defects block a safe demo deployment. Each is detailed in `CRITICAL_FINDINGS.md` with file:line evidence, impact, correction, and required regression tests. Summary:

| # | Defect | One-line impact |
|---|---|---|
| P0-1 | ToolGateway bypassed for all 4 external side effects | Gate 2 approval recorded but never enforced at send site; modified payloads still send |
| P0-2 | Tally webhook HMAC skippable by default | Open unsigned lead ingestion → cost abuse + open email relay |
| P0-3 | No Resend demo-mode recipient restriction | Demo sends real outreach to arbitrary addresses; `opsmesh.dev` sender triggers 403 |
| P0-4 | HubSpot mutations live by default, no off switch | Demo mutates a real CRM; kill switch doesn't gate CRM |
| P0-5 | No rate limiting on any public write endpoint | Cost-abuse surface wide open |
| P0-6 | Unauthenticated public endpoints hardcoded to workspace `"default"` | Anyone creates/reads leads, views dashboard/metrics |
| P0-7 | Sent emails + lead history never persisted | Activity tab + agent memory wiped on every redeploy |
| P0-8 | CRM outbox + 3 reconcilers entirely unwired (dead code) | `SEND_UNKNOWN` never resolves; 24h approvals never expire; outbox never drains |

**All eight are addressed in Stage B Phases 1–4 below.** None require rewriting the state machine, the approval gates, the provider interfaces, or the CRM outbox architecture — they require **wiring existing code in**.

---

## 3. Suspected risks

Three items require verification before Stage B (they are not confirmed defects but could become P0/P1 if real):

1. **Worker free-tier sleep + in-process worker interaction.** When `OPSMESH_WORKER_IN_PROCESS=auto` resolves to True, the API process runs the worker. On Render, the API sleeps after 15 min inactivity. Verify whether the in-process worker also sleeps. (Likely fine — `render.yaml` uses the separate worker service — but confirm `auto` resolves to False on the Render web service.)
2. **Postgres free-tier connection limits.** `asyncpg` pool size is not explicitly configured. Verify the default pool doesn't exhaust under concurrent webhook bursts (free Postgres caps connections).
3. **`SCHEMA_POSTGRES` vs `SCHEMA_SQLITE` drift.** Both live in `adapters/db/schema.py`. The audit found `workspace_members` added to one path. A migration test must diff them and confirm identical table sets.

These are verification tasks for the first day of Stage B Phase 1, not separate phases.

---

## 4. Security assessment

Detailed in `CRITICAL_FINDINGS.md` (P0-1 through P0-8, P1-1 through P1-8, P2-* security items) and `DEPLOYMENT_BASELINE.md` (§6 CORS, §8 secrets).

**Security posture today:**
- ✅ Generic webhook HMAC is enforced and fail-closed.
- ✅ No secrets committed; `render.yaml` marks all keys `sync: false`.
- ✅ Integrations endpoint returns `api_key_configured: bool` only.
- ✅ Global exception handler sanitizes responses.
- ❌ ToolGateway is decorative (P0-1).
- ❌ Tally webhook accepts unsigned POSTs (P0-2).
- ❌ No rate limiting (P0-5).
- ❌ Unauthenticated public endpoints with shared workspace (P0-6).
- ❌ Wildcard CORS + credentials (P1-1).
- ❌ No Resend recipient restriction (P0-3) / no HubSpot demo guard (P0-4).
- ❌ Exception-text echo in auth + provider errors (P2-6) — partly mitigated by OAuth removal.

**Target security posture (end of Stage B):**
- `OPSMESH_ACCESS_MODE=single_user_demo` gates all unauthenticated access to a single server-bound workspace.
- ToolGateway enforced on `send_email` and `create_lead` (the two NOTIFY/WRITE tools) with `verify_and_consume` against the real `run.pending_approval_id`.
- Tally HMAC unconditional + fail-closed in prod mode.
- `slowapi` rate limits on all public write endpoints.
- Resend restricted to `OPSMESH_EMAIL_TEST_RECIPIENT` in demo mode; default sender `onboarding@resend.dev`.
- HubSpot mutations replaced by outbox writes in demo mode (`OPSMESH_CRM_DEMO_MODE=read_only`).
- CORS tightened to the API's own origin; credentials dropped (no OAuth).
- `last_error` scrubbed of provider response bodies before returning to clients.

---

## 5. Architecture assessment

**What is architecturally sound (preserve):**
- The 16-state machine and its transition table (`state_machine.py`). The state design correctly separates Gate 1 (`AWAITING_EVIDENCE_REVIEW` — never authorizes SENDING) from Gate 2 (`AWAITING_APPROVAL`), correctly handles `SEND_UNKNOWN` as a paused state with reconciler-only exits, and correctly fails closed on unknown critic verdicts.
- The supervisor's checkpoint-before-and-after-each-step design (`supervisor.py:243`). This is genuinely restart-safe.
- The ApprovalGate + payload-hash design (`approval_gate.py`, `approval_store.py:compute_payload_hash`, `TOOL_PROTECTED_FIELDS`). The binding is correct; only the enforcement at the send site is missing.
- The provider abstraction (`create_*_provider` factory + mock/real split). Clean and testable.
- The CRM outbox + drainer + backoff design (`reconciler/crm_outbox.py`). The pattern is correct; only the wiring is missing.
- The three reconciler designs (`EmailSendReconciler`, `ApprovalTimeoutReconciler`, `CRMOutboxDrainer`). Each has the right semantics; only the instantiation and the `_runs` private-attr bug need fixing.
- The API + worker shared-database contract (render.yaml wires both to `opsmesh-db`).

**What is architecturally weak (fix, don't rewrite):**
- The supervisor calls specialists directly with `ctx=None` (`supervisor.py:283-285`). Fix: inject a `SecurityContext` and route NOTIFY/WRITE side effects through `ToolGateway.execute`.
- Module-level singletons in every route file. Fix: move to `app.state` (P2-1).
- Sync `sqlite3` mixed into async stores (`SQLiteApprovalStore`, `SQLiteOutboxStore`). Fix: migrate to `aiosqlite` (P1-7).
- `EmailSendReconciler` pokes `InMemoryStateStore._runs`. Fix: add `list_in_state(state)` to the ABC (P2-2).
- Two schema sources of truth (Alembic vs runtime DDL). Fix: pick Alembic (P3-11).

**What must NOT be rewritten (mandate non-negotiable):**
- The state machine.
- Gate 1 and Gate 2.
- The CRM outbox + reconciliation architecture.
- Idempotency and budget controls.
- Provider interfaces.
- API contracts (backward-compatible changes only).
- The mock/real provider split.

---

## 6. Frontend assessment

Detailed in `BASELINE_INVENTORY.md` §11 and `CRITICAL_FINDINGS.md` P1-3 through P1-8, P2-7, P2-8, P2-10.

**Current state:** vanilla HTML/CSS/JS, no build step, 8 pages, 6 nav items. Genuinely functional but visually unfinished (letter-based icons, hyphen empty-states, asterisk brand mark, single 768px breakpoint, borderline WCAG contrast).

**Required nav coverage gap:**
- Overview ✅ — exists, but no charts/trend visualization (P2-8).
- Leads ⚠️ — list only; **no detail view**; table rows not clickable (P1-3).
- Runs ✅ — list + detail, solid.
- Approvals ✅ — list + detail, Gate 1 + Gate 2, solid.
- Activity ⚠️ — split into two tabs, not a unified feed (P1-8).
- Integrations ⚠️ — abstract role names, no Tally, no Worker (P1-5).
- Settings ❌ — **entirely missing** (P1-4).

**Recommendation: do NOT migrate to a frontend framework.** The existing vanilla JS is small (≈700 lines across 2 files), has no build step, serves from the API's own `/ui` mount (same-origin — solves CORS), and every page works. A React/Next migration would:
- Require a build step (Render free web service supports it but adds cold-start time).
- Introduce a second origin (re-introduces the CORS problem).
- Require re-implementing every workflow.
- Risk maintaining two competing frontends.

The mandate says: "Do not migrate simply because a framework is fashionable." **Decision: improve the existing vanilla JS in place.** Stage B Phase 7 redesigns the CSS design system, adds the missing pages (lead detail, settings), unifies Activity, fixes iconography, and adds real charts — all on the existing stack. If the user later wants a framework migration, it is a separate project with its own rollback strategy.

---

## 7. Database and deployment assessment

Detailed in `DEPLOYMENT_BASELINE.md` and `DEPENDENCY_MAP.md`.

**Database:**
- Render Postgres free tier, shared by API + worker. ✅
- Runtime schema created by `connection.py:_init_sqlite_schema` (`CREATE TABLE IF NOT EXISTS`); Alembic has 1 migration that drifts from runtime DDL. **Fix: make Alembic the source of truth, add `preDeployCommand: alembic upgrade head`.**
- `sent_emails` and `lead_history_events` tables exist in the schema but no Python store reads from them. **Fix: implement `DatabaseSentEmailStore` and `DatabaseLeadHistoryStore`.**
- `audit_events` and `crm_outbox` tables exist but are never written to. **Fix: wire ToolGateway audit logger and `CRMUpdatingSpecialist` outbox writes.**
- Sync `sqlite3` in async stores (P1-7). **Fix: migrate to `aiosqlite`.**

**Deployment:**
- `render.yaml` is structurally correct (API + worker + DB, shared connection string, health check path). ✅
- Env-var drift: 13 mismatches documented in `DEPLOYMENT_BASELINE.md` §2. The most dangerous are `OPSMESH_EMAIL_KILL_SWITCH="false"` (P0-3), missing `OPSMESH_TALLY_WEBHOOK_SECRET` (P0-2), missing worker `OPSMESH_PRODUCTION_MODE` (P2-5), wildcard CORS (P1-1), and the `opsmesh.dev` unverified sender default (P1-2 / user's 403).
- Free-tier limitations: 90-day Postgres deletion, web service sleep, no persistent disk. All acceptable for demo scope as long as in-memory stores are replaced (Phase 4).

**No infrastructure plan change required.** All fixes are env-var edits + one `preDeployCommand` + dep re-pin. No paid services introduced.

---

## 8. Research findings from comparable products

> Note: this section describes widely-known capability patterns of B2B sales/intelligence platforms as general industry context for feature-gap analysis. It is not a cite of specific vendor facts or numbers; treat it as design reference, not as claims about any specific product's current shipping feature set.

The mandate asks to "study the interaction patterns of modern B2B sales intelligence, CRM and agent operations platforms." The relevant categories and what OPSMESH should learn from each:

**Category 1 — Sales intelligence / enrichment (Clay, Apollo, Common Room, ZoomInfo).**
- Pattern: every enriched field is backed by a source URL + retrieval timestamp; confidence is shown per-field, not as a single number; stale data is flagged.
- OPSMESH gap: `ResearchSpecialist` produces structured output but the UI does not surface per-field source links or freshness. **Stage B Phase 7: lead detail page shows evidence table with source + date + reliability.**

**Category 2 — Sequence/outreach (Outreach, Salesloft, Lemlist).**
- Pattern: every send is bound to an immutable approved step; editing a step after approval invalidates it; suppression and unsubscribe are enforced at the send boundary, not at sequence creation.
- OPSMESH gap: the approval payload hash exists but is not enforced at send (P0-1). Suppression is checked at intake only, not at send (P0-1 bypass #5). **Stage B Phase 2 + Phase 4 fix both.**

**Category 3 — CRM (HubSpot, Salesforce).**
- Pattern: writes go through an outbox + reconciliation queue; transient failures retry with backoff; duplicate contacts are detected by email before create; associations are explicit.
- OPSMESH gap: the outbox architecture exists but is unwired (P0-8); `CRMUpdatingSpecialist` calls the CRM directly. **Stage B Phase 3 wires it in.**

**Category 4 — Agent operations / observability (LangSmith, Helicone, Arize, Humanloop).**
- Pattern: every run has a traceable timeline with LLM latency, token usage, cost, tool calls, retries; p50/p95/p99 are labeled with sample size; "insufficient data" is shown rather than fake 100%.
- OPSMESH gap: the metrics module computes these but the dashboard shows them as flat tiles without sample-size context or trend (P2-8). **Stage B Phase 7 adds charts + sample-size labels + "insufficient data" state.**

**Category 5 — Operator consoles (Retool, Plane, Linear).**
- Pattern: dense information layout, keyboard shortcuts, command palette, excellent empty/error/loading states, consistent iconography.
- OPSMESH gap: single 768px breakpoint, letter icons, hyphen empty-states, no keyboard shortcuts. **Stage B Phase 7 redesign.**

**What OPSMESH should NOT copy:**
- Generic AI chat interfaces (the mandate explicitly forbids a generic chat).
- Unbounded browser agents (forbidden).
- Autonomous multi-agent demos that don't serve the lead-to-opportunity flow (forbidden).

**Net gap analysis:** OPSMESH's architecture already encodes the right patterns (evidence-aware research, two-gate approval, payload-hash binding, outbox + reconciliation, 16-state recovery). The gap is **enforcement and surfacing**, not architecture. This confirms the Stage B strategy: wire existing architecture in, don't rewrite.

---

## 9. Recommended target architecture

The target architecture is the **existing architecture, wired in**. No new top-level components; no service split; no framework migration.

```
HTTP intake (webhook HMAC / Tally HMAC / manual / simulate)
   │  rate-limited (slowapi); workspace server-bound in demo mode
   ▼
LeadIntakeService.create_lead
   │  durable lead row + RunRecord(NEW) + durable job enqueue
   │  suppression checked (DB-backed ContactPolicy)
   ▼
JobQueue (SQLite/Postgres)  ◄── shared by API in-process worker + Render worker
   │
   ▼
Worker.claim  ──►  Supervisor.run
   │
   │  checkpoint before/after each step (existing, sound)
   │
   ▼
_execute_step(run, specialist, ctx=SecurityContext)   ◄── PHASE 2 CHANGE: ctx no longer None
   │
   │  specialists that do NOTIFY/WRITE side effects call ToolGateway.execute(...)
   │  instead of the provider directly:
   │    - SendingSpecialist   → gateway.execute("send_email", approval_id=run.pending_approval_id, payload)
   │    - CRMUpdatingSpecialist → gateway.execute("create_lead", ...) OR writes CRMOutboxOp
   │  READ side effects (Exa, Groq) keep calling providers directly (documented decision)
   │
   │  ToolGateway.execute enforces: allowlist, approval verify_and_consume + payload_hash,
   │    workspace+resource scope, rate limit, idempotency, audit log, arg schema, suppression
   │
   ▼
Provider adapter (Groq / Exa / Resend / HubSpot) — unchanged interface
   │
   ▼
Specialist returns result → Supervisor applies transition (existing, sound)
   │
   │  on SEND_UNKNOWN → reconciler polls Resend get_status(message_id) until conclusive
   │  on CRM_UPDATING → CRMOutboxDrainer drains PENDING ops with backoff
   │  on AWAITING_APPROVAL past 24h → ApprovalTimeoutReconciler → APPROVAL_EXPIRED
   │
   ▼
StateStore (SQLite/Postgres) — runs, jobs, approvals, leads, idempotency,
   crm_outbox, sent_emails (NEW DB store), lead_history_events (NEW DB store),
   audit_events (NEW writes from gateway), contact_policy (NEW table)
   │
   ▼
API routes (auth-free in demo mode, workspace server-bound) → Dashboard / Leads / Runs /
   Approvals / Activity / Integrations / Settings  (frontend reads from these)
```

**Key architectural decisions (each justified, not fashionable):**

1. **ToolGateway governs NOTIFY/WRITE side effects only (send_email, create_lead/update_lead_status), not READ side effects (Exa search, Groq chat).** Rationale: the gateway's value is approval-binding, suppression, and idempotency — all relevant to sends and CRM mutations. Wrapping every LLM call in the gateway adds latency and audit volume without improving safety; the input guardrails (PII redactor, prompt-injection heuristic) are better applied at the specialist prompt-construction boundary, which Phase 2 also wires in. This respects the mandate: "Do not blindly wrap every internal function in the gateway."

2. **CRM writes go through the outbox, not direct provider calls.** Rationale: the outbox + drainer + backoff already exists and is the correct pattern for transient CRM failures. Wiring it in is Phase 3. In demo mode, the drainer writes `SKIPPED_DEMO` ops without calling HubSpot.

3. **Sent emails and lead history become DB-backed.** Rationale: the tables already exist in the schema; only the Python stores are missing. Phase 4 adds `DatabaseSentEmailStore` and `DatabaseLeadHistoryStore` and swaps them in when a DB backend is configured.

4. **OAuth is removed behind `OPSMESH_ACCESS_MODE=single_user_demo`.** The OAuth code stays in the tree (gated by the access mode) so the migration path back to multi-user auth is documented and reversible. The frontend login page is replaced by a direct dashboard redirect.

5. **Frontend stays vanilla JS.** No framework migration. The design system is rebuilt in `app.css` + a small inline-SVG icon set.

6. **Alembic becomes the single schema source of truth.** `render.yaml` gets `preDeployCommand: alembic upgrade head`. Runtime `CREATE TABLE IF NOT EXISTS` is kept as a dev-only convenience (skipped when `production_mode=true`).

---

## 10. Detailed implementation phases

Eight phases, ordered by dependency. Each phase lists its goal, the P0/P1 items it closes, and its acceptance criterion. **No phase begins until the previous one passes its acceptance criterion and the user approves continuation** (per Stage B protocol).

### Phase 1 — Safe-demo foundation (closes P0-2, P0-3, P0-4, P0-5, P0-6, P1-1, P1-2, P2-3, P2-4, P2-5)

**Goal:** make the existing deploy safe to run publicly as a single-workspace demo, without changing workflow behavior.

**Changes:**
- Add `OPSMESH_ACCESS_MODE` (default `single_user_demo`), `OPSMESH_EMAIL_TEST_RECIPIENT`, `OPSMESH_CRM_DEMO_MODE`, `OPSMESH_DEMO_WORKSPACE_ID` to `Settings`.
- Make Tally HMAC unconditional + fail-closed in prod mode (P0-2). Add `tally_webhook_secret` to the prod-mode startup check.
- Add Resend recipient restriction in demo mode (P0-3). Default `workspace_sender_identity` to `OpsMesh <onboarding@resend.dev>`.
- Add HubSpot demo guard (P0-4): in demo mode, `CRMUpdatingSpecialist` skips the live call (Phase 3 replaces this with the outbox properly).
- Add `slowapi` rate limits on `POST /events/*`, `POST /leads`, `POST /demo/simulate` (P0-5).
- Bind unauthenticated endpoints to `OPSMESH_DEMO_WORKSPACE_ID` server-side; never trust browser-supplied `workspace_id` (P0-6).
- Tighten CORS to the API origin; drop `allow_credentials` (P1-1).
- Read `workspace_sender_identity` in `_create_gate2_approval` instead of the hardcoded `bekalu@opsmesh.dev` (P1-2).
- Remove the `WEBHOOK_SECRET` unprefixed fallback; fix docstrings (P2-3).
- Remove `OPSMESH_EMAIL_FROM` from `.env.example`; document `OPSMESH_WORKSPACE_SENDER_IDENTITY` (P2-4).
- Add `OPSMESH_PRODUCTION_MODE=true` to worker env in `render.yaml` (P2-5).
- Repin `sqlalchemy>=2.0,<2.1`; remove `google-generativeai`; add `requirements-dev.txt` (P3-5, P3-6, P3-7).

**Acceptance criterion:** the deploy boots in demo mode, all public write endpoints are rate-limited and workspace-bound, no live email/CRM mutation can happen without explicit non-demo config, and the existing 739 tests still pass.

### Phase 2 — Wire ToolGateway into the execution path (closes P0-1, P1-2 enforcement)

**Goal:** make the policy layer actually govern NOTIFY/WRITE side effects.

**Changes:**
- Add a `ToolGateway` field to `Supervisor` (`supervisor.py:129-141`); construct it in `_build_default_worker()` and `build_in_process_supervisor()` with a DB-backed `AuditLogger`, `IdempotentToolExecutor`, and `RateLimiter`.
- Inject a `SecurityContext` into `_execute_step` (`supervisor.py:283-285`) — `ctx` is no longer `None`.
- Refactor `SendingSpecialist` to call `gateway.execute("send_email", approval_id=run.pending_approval_id, payload={to, from_, subject, body})` instead of `email_provider.send` directly. Pass the **real** `run.pending_approval_id`, not `f"ap_{run_hex}"`.
- Refactor `CRMUpdatingSpecialist` to call `gateway.execute("create_lead", ...)` OR (preferred, Phase 3) write a `CRMOutboxOp`.
- Apply input guardrails (`PIIRedactor`, `PromptInjectionHeuristic`) at specialist prompt-construction (READ side effects stay direct but guarded).
- Fail closed: if the gateway is unavailable in production, `_execute_step` raises instead of bypassing.

**Acceptance criterion:** a Gate 2 approval with a modified body is rejected at send with `ApprovalPayloadMismatchError`; a direct provider call bypassing the gateway raises; the full lead→CRM flow still works end-to-end with the gateway wired in; 739 + new regression tests pass.

### Phase 3 — Wire reconcilers + CRM outbox (closes P0-8, P2-2, P2-9)

**Goal:** make the workflow genuinely recoverable.

**Changes:**
- Fix `EmailSendReconciler` to use `StateStore.list_in_state(State.SEND_UNKNOWN)` instead of poking `_runs` (P2-2). Add `list_in_state` to the `StateStore` ABC + both impls.
- Fix `SendingSpecialist` to persist `email_message_id` even on TimeoutError (so the reconciler can poll).
- Instantiate `EmailSendReconciler`, `ApprovalTimeoutReconciler`, `CRMOutboxDrainer` in `worker.py:_build_default_worker()` and in `build_in_process_supervisor()`. Run them on a polling loop (every 30s) alongside the job claim loop.
- Make `CRMUpdatingSpecialist` write a `CRMOutboxOp` (status `PENDING`) instead of calling the CRM directly. The drainer drains it. In demo mode, the drainer writes `SKIPPED_DEMO`.
- Add `/api/v1/worker/status` endpoint (last-claim, processed/failed counts, queue depth, DLQ depth, reconciler last-run).

**Acceptance criterion:** a `SEND_UNKNOWN` run resolves to `SENT` or `FAILED` after the reconciler polls; a transient CRM failure retries with backoff and dead-letters after max attempts; a 24h-pending approval expires; the reconciler works with both `InMemoryStateStore` and `SQLiteStateStore`; 739 + new regression tests pass.

### Phase 4 — Durable persistence + store hygiene (closes P0-7, P1-7, P2-1)

**Goal:** data survives restarts; stores are async-clean.

**Changes:**
- Implement `DatabaseSentEmailStore` reading from the existing `sent_emails` table; swap in `create_app()` when DB configured.
- Implement `DatabaseLeadHistoryStore` reading from `lead_history_events`.
- Persist `ContactPolicy` (suppression list) to a new `contact_policy` table; `LeadIntakeService` reads from it instead of the in-memory `_suppression`.
- Persist the audit log: `ToolGateway` writes to `audit_events` via a `DatabaseAuditLogger` (Phase 2 constructs the gateway; Phase 4 swaps the logger).
- Migrate `SQLiteApprovalStore` and `SQLiteOutboxStore` sync `sqlite3` calls to `aiosqlite` (P1-7).
- Move route module-level singletons onto `app.state` (P2-1).

**Acceptance criterion:** after a simulated worker restart (kill mid-run, restart), sent emails, lead history, suppression, and audit log are all intact; no async store blocks the event loop; `create_app()` is safe to call twice in one process; 739 + new regression tests pass.

### Phase 5 — Remove Google OAuth (closes P1-6, P3-1)

**Goal:** visiting `/` opens the dashboard directly; no Google login.

**Changes:**
- Add `OPSMESH_ACCESS_MODE=single_user_demo` as the gate. When set, `get_current_user` returns the demo workspace identity without checking a JWT; OAuth routes return 410 Gone or are excluded from the router.
- Remove `auth_google_router` from `app.py:343` when `access_mode != multi_user`. Keep `auth_google.py` in the tree (gated) for the future multi-user path.
- Remove `login.html`; make `index.html` redirect to `/ui/dashboard.html` directly.
- Remove `OPSMESH_GOOGLE_OAUTH_*` and `OPSMESH_SESSION_SECRET` from `render.yaml`.
- Remove the GIS script tag, the Google button, `loginWithGoogle`, localStorage token storage, and the `mode: dev` sidebar indicator from `js/app.js` + `js/shell.js`.
- Scrub `last_error` of provider response bodies (P2-6) — partly addressed by removing the auth error paths.

**Acceptance criterion:** visiting `/` opens the dashboard with no login; no Google OAuth button, token, or redirect anywhere; the future multi-user path is documented and the OAuth code remains in tree behind the access-mode flag; 739 + new regression tests pass.

### Phase 6 — Evaluation expansion (mandate: 30+ scenarios)

**Goal:** expand the eval harness beyond happy-path.

**Changes:**
- Extend `opsmesh/eval/runner.py` + `mocks.py` to cover 30 scenarios across: normal lead processing, insufficient research, conflicting evidence, unsupported claims, qualification mistakes, prompt injection, malicious URLs, invalid input, duplicate webhooks, duplicate tool calls, invalid approval, expired approval, modified approved payload, rejected email, email timeout, `SEND_UNKNOWN` recovery, CRM timeout, CRM duplicate prevention, database failure, worker restart, queue recovery, budget exhaustion, rate limiting, workspace boundary violations, ToolGateway bypass attempts, provider outages.
- Measure: qualification accuracy against labeled examples, evidence grounding, citation validity, hallucination rate, Critic detection effectiveness, approval invariant enforcement, idempotency, recovery correctness, cost, latency.
- Deterministic mocks for CI; live provider tests opt-in (`tests/integration/`, behind API keys).
- Add eval report output (markdown + JSON) for Stage C acceptance.

**Acceptance criterion:** `pytest -m eval` runs 30+ scenarios, all pass with deterministic mocks, and the eval report is generated; 739 prior tests unaffected.

### Phase 7 — Frontend redesign (closes P1-3, P1-4, P1-5, P1-8, P2-7, P2-8, P2-10)

**Goal:** premium B2B SaaS product experience on the existing vanilla JS stack.

**Changes:**
- Rebuild `app.css` design system: dark navy/charcoal palette, restrained blue/cyan accents, type scale, consistent spacing rhythm, refined borders/shadows, accessible contrast (≥4.5:1), responsive breakpoints (desktop/laptop/tablet/mobile).
- Adopt a small inline-SVG icon set (Lucide, MIT) for nav, events, empty states, buttons.
- Add **Leads detail view** (`leads.html?id=...` branch): company/contact info, qualification score breakdown, evidence table with source links + dates + reliability, research findings, run history, CRM sync status. Make table rows clickable.
- Add **Settings / Operations page** (`settings.html`): read-only config validation, `email_kill_switch` toggle (demo-policy-gated), audit log viewer, worker status, demo-data reset (guarded).
- Add **Integrations** vendor names (Groq/Exa/Resend/HubSpot), Tally card, Worker card.
- Unify **Activity** into a single chronological feed with type/run/lead/status filters; surface per-lead activity.
- Add **dashboard charts**: runs-over-time, success-rate-over-time (with sample-size label + "insufficient data" state), cost-over-time; date-range filter.
- ARIA roles on tabs, skip-to-content link, `aria-live` on toasts.

**Acceptance criterion:** every required nav item exists and works; every button/form/filter has a backend endpoint; no fake charts or fake data; visual check (rendered pages) passes the mandate's quality bar; smoke tests pass.

### Phase 8 — Optional enhancements (include only the justified ones)

**Included (justified by the mandate):** configurable ICP (Settings UI writes a `icp_config` row read by `QualifySpecialist`); lead prioritization (sort by score); draft version history (persist draft snapshots); suppression management UI; DLQ retry button; demo-data seeding + reset; audit history viewer; system config validation on boot.

**Deferred (not justified for demo):** buying-signal detection, research refresh, email templates/tone config, CRM field mapping, CRM sync conflict detection, exportable run reports. Each is listed in §17 with its deferral rationale.

**Acceptance criterion:** each included enhancement has a clear business value, a test, and no scope creep into the deferred list.

---

## 11. File-by-file expected changes

Compact map of every file Stage B touches. **All changes are backward-compatible** unless marked `[BREAKING]` (and breaking changes require a migration + user approval).

### Phase 1 (safe-demo foundation)
- `opsmesh/config.py` — add `access_mode`, `email_test_recipient`, `crm_demo_mode`, `demo_workspace_id` fields; default `workspace_sender_identity` to `OpsMesh <onboarding@resend.dev>`.
- `opsmesh/api/routes/tally_webhook.py` — unconditional HMAC; `EmailStr` validation on `contact_email`.
- `opsmesh/api/app.py` — add `tally_webhook_secret` to prod-mode startup check; tighten CORS; drop `allow_credentials`.
- `opsmesh/specialists/__init__.py` — `SendingSpecialist` demo-recipient check; `CRMUpdatingSpecialist` demo-skip (Phase 3 replaces).
- `opsmesh/api/routes/leads.py`, `dashboard.py`, `metrics.py`, `webhook.py`, `tally_webhook.py` — bind to `demo_workspace_id` server-side; add rate limits.
- `opsmesh/supervisor.py:550` — read `workspace_sender_identity` for Gate 2 payload.
- `opsmesh/webhook.py` — remove `WEBHOOK_SECRET` fallback; fix docstrings.
- `requirements.txt` — repin sqlalchemy, remove google-generativeai.
- `requirements-dev.txt` — new file (ruff, mypy).
- `.env.example`, `render.yaml` — env-var edits per `DEPLOYMENT_BASELINE.md` §10.

### Phase 2 (ToolGateway wiring)
- `opsmesh/supervisor.py` — add `gateway` field; inject `SecurityContext` into `_execute_step`.
- `opsmesh/specialists/__init__.py` — `SendingSpecialist` and `CRMUpdatingSpecialist` call `gateway.execute(...)`; apply input guardrails at prompt construction.
- `opsmesh/worker.py` — construct `ToolGateway` in `_build_default_worker()` and `build_in_process_supervisor()`.
- `opsmesh/tool_gateway.py` — no structural change; possibly add demo-mode policy.

### Phase 3 (reconcilers + outbox)
- `opsmesh/state_store.py` — add `list_in_state(state)` to ABC.
- `opsmesh/adapters/db/sqlite_state_store.py`, `opsmesh/state_store.py` — implement `list_in_state`.
- `opsmesh/reconciler/email_send.py` — use `list_in_state`; fix `_runs` private-attr access.
- `opsmesh/specialists/__init__.py:777` — persist `email_message_id` on TimeoutError.
- `opsmesh/specialists/__init__.py:807` — write `CRMOutboxOp` instead of direct CRM call.
- `opsmesh/worker.py` — instantiate + start 3 reconcilers on a polling loop.
- `opsmesh/api/routes/integrations.py` (or new `worker_status.py`) — add `/api/v1/worker/status`.

### Phase 4 (durable persistence)
- `opsmesh/sent_email_store.py` — add `DatabaseSentEmailStore` (or new `adapters/db/sqlite_sent_email_store.py`).
- `opsmesh/lead_history_store.py` — add `DatabaseLeadHistoryStore`.
- `opsmesh/services/lead_intake.py` — read suppression from `ContactPolicy` DB store.
- `opsmesh/adapters/db/` — new `contact_policy_store.py`; `audit_event_store.py`.
- `opsmesh/adapters/db/sqlite_approval_store.py`, `sqlite_outbox_store.py` — migrate sync→aiosqlite.
- `opsmesh/api/routes/*.py` — move module singletons to `app.state`.
- `opsmesh/adapters/db/schema.py` — ensure `contact_policy`, `audit_events` tables match models; add migration.

### Phase 5 (OAuth removal)
- `opsmesh/api/app.py:343` — gate `auth_google_router` inclusion on `access_mode`.
- `opsmesh/api/auth.py:99-114` — `get_current_user` returns demo identity when `access_mode=single_user_demo`.
- `opsmesh/static/login.html` — remove (or replace with a redirect stub).
- `opsmesh/static/index.html` — redirect to `/ui/dashboard.html`.
- `opsmesh/static/js/app.js` — remove `loginWithGoogle`, localStorage token, `mode: dev` indicator.
- `opsmesh/static/js/shell.js` — remove OAuth-related sidebar elements.
- `render.yaml` — remove `OPSMESH_GOOGLE_OAUTH_*`, `OPSMESH_SESSION_SECRET`.
- `opsmesh/supervisor.py:333-340` + `opsmesh/api/routes/runs.py` — scrub `last_error`.

### Phase 6 (eval expansion)
- `opsmesh/eval/runner.py`, `mocks.py` — 30 scenarios.
- `tests/eval/` — new scenario files.
- `opsmesh/eval/report.py` — new eval report generator.

### Phase 7 (frontend)
- `opsmesh/static/css/app.css` — full design-system rebuild.
- `opsmesh/static/js/icons.js` — new inline-SVG icon set.
- `opsmesh/static/leads.html`, `js/leads.js` — lead detail view.
- `opsmesh/static/settings.html`, `js/settings.js` — new page.
- `opsmesh/static/integrations.html` — vendor names, Tally, Worker.
- `opsmesh/static/activity.html` — unified feed + filters.
- `opsmesh/static/dashboard.html` — charts + date-range + sample-size labels.
- `opsmesh/static/js/shell.js` — add Settings nav item.
- `opsmesh/api/routes/dashboard.py` — date-range param + time-series data.

### Phase 8 (optional enhancements)
- `opsmesh/specialists/__init__.py` (`QualifySpecialist`) — read `icp_config` from DB.
- `opsmesh/api/routes/settings.py` — new ICP config + suppression + audit endpoints.
- `opsmesh/api/routes/runs.py` — DLQ retry endpoint.

---

## 12. Dependency changes

See `DEPENDENCY_MAP.md` §7 for the full table. **Summary: no new runtime dependencies for Phases 1–7** (ToolGateway wiring, durable stores, reconcilers, OAuth removal, demo guards, frontend, eval expansion all build on FastAPI + Pydantic + SQLAlchemy + aiosqlite + httpx + vanilla JS already in the tree).

**One new runtime dependency in Phase 1:** `slowapi` (rate limiting). Justification: the mandate requires rate limiting on public write endpoints; `slowapi` is the standard FastAPI rate limiter, MIT-licensed, no transitive heavy deps. Alternative: hand-rolled in-memory limiter (already half-built in `security_context.py:InMemoryRateLimiter`) — but per-process only and not appropriate for the public write surface. `slowapi` with an in-memory backend is acceptable for the single-instance demo; a Redis backend is documented as the future-prod path.

**Dev dependency additions:** `ruff`, `mypy` in `requirements-dev.txt`.

**Removals:** `google-generativeai` (wrong provider, unused).

**Repins:** `sqlalchemy==2.1.8` → `sqlalchemy>=2.0,<2.1`.

---

## 13. Database migration strategy

**Principle:** backward-compatible migrations only; no data destruction; no production reset.

**Step 1 (Phase 1):** no schema change. Env-var + code-only.

**Step 2 (Phase 3):** no schema change. The `crm_outbox` table already exists; Phase 3 only starts writing to it.

**Step 3 (Phase 4):** new migration `002_add_contact_policy_and_audit_indexes.py`:
- `CREATE TABLE contact_policy (...)` (id, workspace_id, contact_email, suppressed, suppressed_at, reason).
- `CREATE TABLE audit_events (...)` — the table exists in the runtime schema but not in Alembic; add it to Alembic. Add indexes on `(workspace_id, created_at)`, `(run_id)`.
- Verify `sent_emails` and `lead_history_events` tables match model fields; add any missing columns.

**Step 4 (Phase 5):** no schema change.

**Step 5 (Phase 8):** `003_add_icp_config.py` — `CREATE TABLE icp_config (...)`.

**Migration safety:**
- Every migration is `render_as_batch=True` (SQLite-compatible) and reversible (downgrade path).
- `preDeployCommand: alembic upgrade head` in `render.yaml` runs before the app starts.
- A migration test (`tests/test_migrations.py`) verifies `alembic upgrade head` is idempotent and `alembic downgrade -1` restores the prior state.
- The runtime `CREATE TABLE IF NOT EXISTS` is kept for dev convenience but skipped when `production_mode=true` (Alembic is the source of truth in prod).

**Rollback:** `alembic downgrade -1` per migration. Documented in `DEPLOYMENT_BASELINE.md` §10.

---

## 14. Regression test plan

**Baseline gate (every phase):** `python -m pytest tests/ -q --ignore=tests/integration` must report **739 passed, 0 failed, 0 errored, 0 warnings**. Any deviation stops the phase.

**New regression tests added per phase (estimated counts):**

| Phase | New tests | Focus |
|---|---|---|
| 1 | ~25 | demo-mode guards, Tally HMAC fail-closed, rate limiting, workspace binding, sender identity |
| 2 | ~15 | ToolGateway enforcement on send/create, payload-hash rejection, bypass-attempt rejection, fail-closed |
| 3 | ~12 | SEND_UNKNOWN resolution, CRM outbox drain + retry + DLQ, approval timeout, reconciler works with SQLite |
| 4 | ~10 | sent-email/history survive restart, suppression persistence, audit log writes, async store non-blocking, app.state isolation |
| 5 | ~8 | no OAuth routes in demo mode, dashboard opens at `/`, no token storage, last_error scrubbed |
| 6 | 30+ | eval scenarios (deterministic mocks) |
| 7 | ~10 | smoke tests (page load, filter, chart render with real data, empty state) |
| 8 | ~8 | ICP config persistence, DLQ retry, demo reset |
| **Total new** | **~120** | |

**Target suite size after Stage B:** ~860 tests + 30 eval scenarios.

**Coverage gate:** `--cov-fail-under=80` (Makefile) maintained or raised.

**Live provider tests** (`tests/integration/`) remain opt-in, behind real API keys, never in CI.

**Stage C acceptance test (the full workflow):**
`Tally intake → durable lead → durable job → research → qualify → Gate 1 (insufficient evidence) → request_more_info → research refresh → Gate 1 proceed → draft → critic (reject) → draft retry → critic (approve) → Gate 2 (edit then approve) → suppression check → ToolGateway enforcement → Resend test delivery to test recipient → delivery status handling → HubSpot outbox write → CRM sync → dashboard + activity history.`

This runs as a single integration test with deterministic mocks, plus a manual run with real Resend `onboarding@resend.dev` + mock CRM for the demo acceptance.

---

## 15. Deployment and rollback plan

Detailed in `DEPLOYMENT_BASELINE.md` §10. **Summary:**

**No infrastructure plan change.** Render free tier (1 Postgres, 1 web, 1 worker) is sufficient.

**Deployment changes (all env-var + one preDeployCommand):**
1. Add `OPSMESH_ACCESS_MODE=single_user_demo` (both services).
2. Add `OPSMESH_EMAIL_TEST_RECIPIENT=<your email>` (both services).
3. Add `OPSMESH_CRM_DEMO_MODE=read_only` (both services).
4. Set `OPSMESH_EMAIL_KILL_SWITCH=true` until demo verified (then flip to `false`).
5. Set `OPSMESH_CRM_PROVIDER=mock` for demo (then `hubspot` for prod).
6. Set `OPSMESH_WORKSPACE_SENDER_IDENTITY=OpsMesh <onboarding@resend.dev>`.
7. Add `OPSMESH_TALLY_WEBHOOK_SECRET` (both services).
8. Add `OPSMESH_PRODUCTION_MODE=true` to worker.
9. Tighten `OPSMESH_CORS_ALLOWED_ORIGINS` to the API origin.
10. Remove `OPSMESH_GOOGLE_OAUTH_*`, `OPSMESH_SESSION_SECRET`.
11. Add `preDeployCommand: alembic upgrade head` (API service).

**Rollback procedure (per change):** revert the env var in the Render dashboard; redeploy. No data loss (migrations are backward-compatible; `alembic downgrade -1` is documented). The OAuth code stays in tree, so re-enabling multi-user auth is a config flip.

**Phase-by-phase deployment:** each phase is deployed independently after its acceptance criterion passes. No big-bang deploy. The user approves continuation between phases (Stage B protocol).

**Verification after deploy:**
- `/health` returns 200; `/ready` returns 200 (and actually probes the DB after Phase 7).
- `/api/v1/integrations` shows all providers configured/connected.
- Submit a test lead via the Tally webhook (signed) → see it in `/api/v1/leads` → see the run progress in `/api/v1/runs/{id}/timeline`.
- Approve Gate 2 → email arrives at `OPSMESH_EMAIL_TEST_RECIPIENT` from `onboarding@resend.dev`.
- Kill the worker mid-run → restart → run resumes from checkpoint.
- Check Activity tab after restart → sent email + history intact (after Phase 4).

---

## 16. Estimated complexity and risk per phase

| Phase | Complexity | Risk | Why |
|---|---|---|---|
| 1 — Safe-demo foundation | Medium | Low | Mostly env-var + config edits; one new dep (`slowapi`); no workflow change. |
| 2 — ToolGateway wiring | High | Medium | Touches the supervisor execution path; must preserve the 739-test baseline; fail-closed semantics must be exact. |
| 3 — Reconcilers + outbox | Medium | Medium | Reconcilers exist; wiring is mechanical; the `_runs` private-attr fix is subtle. |
| 4 — Durable persistence | Medium | Low | New stores follow the existing `DatabaseLeadStore` pattern; sync→async migration is well-understood. |
| 5 — OAuth removal | Low | Low | Concentrated in 5 files; access-mode gate is clean; OAuth code stays in tree. |
| 6 — Eval expansion | Medium | Low | Deterministic mocks; no production impact; additive. |
| 7 — Frontend redesign | High | Low (functional) / Medium (visual) | No functional risk (API contracts preserved); visual quality is subjective and needs the visual check loop. |
| 8 — Optional enhancements | Low per item | Low | Each is small and independent. |

**Total estimated effort:** the work is sized in days-per-phase, not weeks — the architecture is already in place; Stage B wires it in. The exact calendar depends on the user's approval cadence between phases.

**Highest-risk phase: Phase 2.** It touches `_execute_step`, which every test exercises indirectly. The mitigation is: (a) wire the gateway as an opt-in per specialist (sending + CRM only), (b) run the full 739-test baseline after every commit in Phase 2, (c) if any baseline test breaks, stop and fix before proceeding.

---

## 17. Features deliberately deferred

Each deferred item, with rationale:

| Feature | Deferred because |
|---|---|
| Buying-signal detection | Requires a reliable real-time data source (news APIs, LinkedIn scraping) — not justified for demo; high cost, low reliability. |
| Research refresh on a schedule | Cost-abuse risk if automated; Gate 1 `request_more_info` covers the human-triggered path. |
| Email templates + tone configuration | Scope creep; the Draft specialist's prompts already encode tone; a template UI adds complexity without demo value. |
| CRM field mapping | HubSpot adapter is sufficient; mapping UI adds complexity for a single-CRM demo. |
| CRM sync conflict detection | Single-writer (the outbox drainer) makes conflicts unlikely; defer until multi-source sync exists. |
| Exportable run reports | Operators can use the run detail page; export is a nice-to-have, not Stage C acceptance. |
| Gmail SMTP adapter | Not justified; Resend covers the demo; SMTP adds auth complexity. |
| Vector database | Not needed; the lead history + evidence tables are sufficient for retrieval. |
| Multi-agent autonomous orchestration | Explicitly forbidden by the mandate. |
| Browser agents | Explicitly forbidden. |
| Generic chat interface | Explicitly forbidden. |
| Microservices split | Single FastAPI + worker is sufficient; split adds operational cost. |

**These may be revisited after Stage C acceptance.** None are required for the demo to meet the mandate.

---

## 18. Phase dependency graph

```
Phase 1 (safe-demo foundation)
   │
   ├──► Phase 5 (OAuth removal)        [can start once Phase 1 lands; independent of 2/3/4]
   │
   ▼
Phase 2 (ToolGateway wiring)           [depends on Phase 1 demo guards + sender identity fix]
   │
   ▼
Phase 3 (reconcilers + outbox)         [depends on Phase 2: CRM specialist no longer calls CRM directly]
   │
   ▼
Phase 4 (durable persistence)          [depends on Phase 3: audit log writes from gateway need DB store]
   │
   ├──► Phase 8 (optional enhancements) [depends on Phase 4 for ICP config persistence]
   │
   ▼
Phase 6 (eval expansion)               [depends on Phases 2-4: tests the wired behavior]
   │
   ▼
Phase 7 (frontend redesign)            [depends on Phase 4: lead detail needs durable history;
   │                                     depends on Phase 3: worker status endpoint]
   ▼
Stage C acceptance
```

**Critical path:** Phase 1 → 2 → 3 → 4 → 6 → 7. Phases 5 and 8 can run in parallel with their predecessors.

**Recommended order of execution:** 1, 5, 2, 3, 4, 6, 7, 8 — with user approval between each.

---

## 19. What I will NOT do in Stage B without re-approval

To honor the mandate's non-negotiable preservation rules and Stage B protocol:

- I will **not** rewrite the 16-state machine.
- I will **not** remove Gate 1 or Gate 2.
- I will **not** remove the CRM outbox or reconciliation architecture (I will wire it in).
- I will **not** remove idempotency or budget controls.
- I will **not** replace provider interfaces.
- I will **not** silently change API contracts (backward-compatible only; breaking changes flagged for approval).
- I will **not** destroy existing database records or reset the production database.
- I will **not** disable webhook signature verification.
- I will **not** bypass ToolGateway in production.
- I will **not** enable unrestricted email sending.
- I will **not** introduce fake production metrics.
- I will **not** report unverified features as implemented.
- I will **not** add dependencies beyond `slowapi` without explaining their purpose.
- I will **not** migrate the frontend to a framework.
- I will **not** make a deployment change before its rollback plan is documented.
- I will **not** proceed to the next phase until the current one passes its acceptance criterion **and** the user approves continuation.

---

## Approval request

This Stage A plan is ready for review. The companion documents (`BASELINE_INVENTORY.md`, `BASELINE_TEST_RESULTS.md`, `CRITICAL_FINDINGS.md`, `DEPENDENCY_MAP.md`, `DEPLOYMENT_BASELINE.md`) provide the evidence base.

**To approve Stage B implementation, reply with:** which phase(s) to start with (recommended: Phase 1 + Phase 5 in parallel, since they are independent), or "approved — proceed in recommended order."

**To request changes, reply with:** the specific phase, file, or decision to reconsider.

No application code will be modified until explicit approval is received.
