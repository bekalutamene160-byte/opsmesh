# OPSMESH Critical Findings

**Audit date:** 2026-11-12
**Repo:** `/home/z/my-project/opsmesh/` (commit `4ec6cdf`)
**Method:** four parallel read-only audits (inventory, workflow trace, frontend, security) cross-referenced against source.

This document lists every confirmed defect, suspected risk and optional enhancement found during Phase 0, with severity, exact file/function, evidence, impact, proposed correction, and the regression tests required. Severities:

- **P0** — blocks a safe demo deployment; security or data-integrity defect that must be fixed before any public deploy.
- **P1** — serious security or functional defect; fix before Stage C acceptance.
- **P2** — hardening gap or correctness hazard; fix during Stage B.
- **P3** — hygiene, drift, or documentation; fix opportunistically.

**Confirmed defects** are marked `[CONFIRMED]` with file:line evidence. **Suspected risks** are marked `[SUSPECTED]` and require verification. **Optional enhancements** are marked `[ENHANCEMENT]`.

---

## P0 — Blocks safe demo deployment

### P0-1. ToolGateway is bypassed for all four external side effects `[CONFIRMED]`

- **File/function:** `opsmesh/tool_gateway.py:265` `ToolGateway.execute` (defined, never invoked in production). Specialists call providers directly:
  - Groq LLM — `opsmesh/specialists/__init__.py:188, 280, 390, 556, 644` (`await self.llm.chat(...)`)
  - Exa research — `opsmesh/specialists/__init__.py:162, 249` (`await self.research.web_search(...)`)
  - Resend email — `opsmesh/specialists/__init__.py:773` (`await self.email_provider.send(msg)`)
  - HubSpot CRM — `opsmesh/specialists/__init__.py:807` (`await self.crm_provider.create_lead(...)`)
- **Evidence:** `grep` for `ToolGateway(` outside tests returns zero hits. `grep` for `gateway.execute(` outside `tool_gateway.py` returns zero hits. `Supervisor` constructor (`supervisor.py:129-141`) has no `ToolGateway` field. `_execute_step` (`supervisor.py:283-285`) calls `await specialist(run, budget=budget, ctx=None)` — `ctx=None` means no `SecurityContext`.
- **Impact:** The 7-check policy layer (tool allowlist, approval verify-and-consume, workspace+resource scope, rate limit, idempotency, audit log, argument schema validation, suppression) is decorative. Gate 2 approval is recorded and decided by a human but **never enforced at the send site** — `SendingSpecialist` synthesizes a fake `approval_id = f"ap_{run_hex}"` (`specialists/__init__.py:763`) derived from `run_id`, not `run.pending_approval_id`. A modified recipient/subject/body after approval would still be sent because no hash comparison runs before the Resend POST.
- **Proposed correction (Stage B Phase 2):** wire `ToolGateway` into the supervisor's `_execute_step` so that the `SendingSpecialist` and `CRMUpdatingSpecialist` obtain a scoped `SecurityContext` and call `gateway.execute("send_email", ...)` / `gateway.execute("create_lead", ...)` instead of the provider directly. The gateway calls `RealApprovalVerifier.verify_and_consume` with the actual `run.pending_approval_id` and the current payload hash. Pass the real `approval_id` from the run, not a synthesized one. Production execution must fail closed if the gateway is unavailable.
- **Regression tests required:**
  - `test_tool_gateway_enforced_on_send_email` — approve a Gate 2 payload, modify the body before send, assert the send is rejected with `ApprovalPayloadMismatchError`.
  - `test_tool_gateway_enforced_on_crm_create` — assert `create_lead` requires a verified approval and workspace scope.
  - `test_tool_gateway_bypass_attempt_rejected` — call the provider directly (bypassing the gateway) and assert it raises.
  - `test_send_with_unverified_approval_fails_closed` — synthesize the old `f"ap_{run_hex}"` approval_id and assert the send is rejected.

### P0-2. Tally webhook HMAC is skippable by default `[CONFIRMED]`

- **File/function:** `opsmesh/api/routes/tally_webhook.py:196` — `if _tally_secret:` guards signature verification. `opsmesh/config.py:108` `tally_webhook_secret` defaults to `""`. `render.yaml` does not set `OPSMESH_TALLY_WEBHOOK_SECRET`. `app.py:96-99` prod-mode fail-closed checks only `webhook_secret`, not `tally_webhook_secret`.
- **Evidence:** with the secret empty, any unsigned POST to `/api/v1/events/tally` is accepted, normalized, and creates a lead + run + job. The 5-min dedup window catches only identical `submission_id`s — rotating the ID floods the Groq + Exa pipeline.
- **Impact:** open unsigned lead ingestion → cost abuse + potential open email relay once Gate 2 is approved (the Tally `contact_email` field has no validation — `tally_webhook.py:105-109` accepts any non-empty string).
- **Proposed correction (Stage B Phase 1):** make the Tally HMAC check unconditional; fail closed (401) when the secret is unset in production mode. Add `tally_webhook_secret` to the `app.py:90-99` prod-mode startup check alongside `webhook_secret`. Add `OPSMESH_TALLY_WEBHOOK_SECRET` to `render.yaml` for both services. Validate `contact_email` with `EmailStr` in `normalize_tally_payload`.
- **Regression tests required:**
  - `test_tally_webhook_rejects_unsigned_post_in_prod_mode`
  - `test_tally_webhook_rejects_invalid_signature`
  - `test_tally_webhook_validates_contact_email`
  - `test_prod_mode_startup_fails_without_tally_secret`

### P0-3. Resend has no demo-mode recipient restriction `[CONFIRMED]`

- **File/function:** `opsmesh/specialists/__init__.py:704-773` — `SendingSpecialist` sends to whatever `lead.contact_email` resolves to. `opsmesh/config.py` has no `email_test_recipient` or `demo_mode` field. `render.yaml:46, 91` set `OPSMESH_EMAIL_KILL_SWITCH="false"`, flipping the only safety off. Default sender identity is `Bekalu <bekalu@opsmesh.dev>` (`config.py:130`) — an unverified Resend domain that triggers the 403 in the user's screenshot.
- **Evidence:** the only gate is `email_kill_switch` (`specialists/__init__.py:744-748`), ON by default in `config.py:102` but OFF in `render.yaml`. No allowlist of test recipients, no sandboxing flag.
- **Impact:** a demo deploy with `render.yaml` as-is sends real outreach emails to arbitrary lead contact_emails the moment a Gate 2 approval is approved. Combined with P0-2 (unsigned Tally) and the unvalidated email field, the demo is an open email relay.
- **Proposed correction (Stage B Phase 1):** add `OPSMESH_ACCESS_MODE=single_user_demo` and `OPSMESH_EMAIL_TEST_RECIPIENT` to `Settings`. In `SendingSpecialist`, when `access_mode == "single_user_demo"`, assert `recipient == email_test_recipient` before send (else raise `DemoRecipientViolation`). Default `render.yaml` to `OPSMESH_EMAIL_KILL_SWITCH=true` and `OPSMESH_WORKSPACE_SENDER_IDENTITY=OpsMesh <onboarding@resend.dev>` (Resend's pre-verified test sender). Document the migration path to a verified custom domain.
- **Regression tests required:**
  - `test_demo_mode_blocks_send_to_non_test_recipient`
  - `test_demo_mode_allows_send_to_test_recipient`
  - `test_kill_switch_true_blocks_all_sends`
  - `test_onboarding_resend_dev_sender_accepted` (integration, opt-in)

### P0-4. HubSpot mutations are live by default with no off switch `[CONFIRMED]`

- **File/function:** `opsmesh/specialists/__init__.py:798-817` `CRMUpdatingSpecialist.__call__` calls `await self.crm_provider.create_lead(...)` directly. `opsmesh/adapters/crm/hubspot.py:147-195` issues a live `POST /crm/v3/objects/contacts`. `render.yaml:48, 93` set `OPSMESH_CRM_PROVIDER=hubspot`. `email_kill_switch` does not gate CRM. The state machine transitions to `CRM_UPDATING` unconditionally after `sent`/`skipped` (`supervisor.py:422-429`) — even kill-switch-skipped runs still mutate HubSpot.
- **Evidence:** no demo flag, no read-only mode, no dry-run.
- **Impact:** the demo deploy mutates a real CRM with no off switch short of setting `OPSMESH_CRM_PROVIDER=mock` (which `render.yaml` does not).
- **Proposed correction (Stage B Phase 1):** add `OPSMESH_CRM_DEMO_MODE=read_only` (or `disabled`) to `Settings`. In `CRMUpdatingSpecialist`, when demo mode is read-only, write a `CRMOutboxOp` with status `SKIPPED_DEMO` instead of calling the provider; transition to `SENT` with a synthetic CRM result. Default `render.yaml` to `OPSMESH_CRM_PROVIDER=mock` for the demo deploy.
- **Regression tests required:**
  - `test_crm_demo_mode_skips_live_mutation`
  - `test_crm_demo_mode_writes_outbox_op_skipped_demo`
  - `test_kill_switch_skipped_send_still_skips_crm_when_demo` (fixes the `CRM_UPDATING`-after-skipped-send bug)

### P0-5. No rate limiting on any public write endpoint `[CONFIRMED]`

- **File/function:** no limiter middleware in `app.py:301-307` (only `CORSMiddleware`); no limiter library in `requirements.txt`. Public write endpoints with zero throttling: `POST /events/new_lead`, `POST /events/tally`, `POST /leads`, `POST /demo/simulate`, `POST /auth/google`, `POST /approvals/{id}/decision`. The `InMemoryRateLimiter` (`security_context.py:243`) and `ToolGateway`'s per-tool `rate_limit_per_min` (`tool_gateway.py:103, 376-381`) are never instantiated in production.
- **Evidence:** `requirements.txt` grep for `slowapi|fastapi-limiter|limiter` returns nothing.
- **Impact:** an attacker rotating `submission_id` floods Groq + Exa (real cost) and the job queue. Combined with P0-2 and the unauthenticated `/leads` + `/demo/simulate` (P1-3), the cost-abuse surface is wide open.
- **Proposed correction (Stage B Phase 1):** add `slowapi` (or an in-tree Redis-free limiter) to `requirements.txt`. Apply a per-IP rate limit (e.g. 10/min) to `POST /events/*`, `POST /leads`, `POST /demo/simulate`. Apply a stricter limit to `/demo/simulate` (e.g. 5/min) since it fabricates leads from no input. Document that the in-memory limiter is per-process; for multi-worker, recommend a Redis-backed limiter in the future-prod path.
- **Regression tests required:**
  - `test_rate_limit_blocks_after_threshold` (per-IP)
  - `test_rate_limit_independent_per_ip`
  - `test_webhook_endpoint_rate_limited`

### P0-6. Unauthenticated public write/read endpoints hardcoded to workspace `"default"` `[CONFIRMED]`

- **File/function:**
  - `opsmesh/api/routes/leads.py:185` `POST /api/v1/leads` (no `Depends(get_current_user)`) — uses `_workspace_id="default"` (`:42-43`)
  - `opsmesh/api/routes/leads.py:217` `GET /api/v1/leads/{id}` (no auth) — uses `_workspace_id`
  - `opsmesh/api/routes/leads.py:243` `POST /api/v1/demo/simulate` (no auth) — uses `_workspace_id`
  - `opsmesh/api/routes/dashboard.py:47` `GET /api/v1/dashboard` (no auth) — uses `_workspace_id="default"` (`:35`)
  - `opsmesh/api/routes/metrics.py:38` `GET /api/v1/metrics` (no auth) — uses `_workspace_id="default"` (`:26`)
  - `opsmesh/api/routes/webhook.py:147` hardcodes `workspace_id="default"` (comment: "Day 6: derive from webhook secret" — never implemented)
  - `opsmesh/api/routes/tally_webhook.py:222` hardcodes `workspace_id="default"`
- **Evidence:** function signatures lack `Depends(get_current_user)`; `_workspace_id` is a module-level default.
- **Impact:** anyone can create leads, read any lead by ID, view the dashboard and metrics of the default workspace. In a multi-tenant future this is a cross-workspace isolation failure. For the demo (single workspace) it is an information-disclosure and abuse vector.
- **Proposed correction (Stage B Phase 1):** introduce `OPSMESH_ACCESS_MODE=single_user_demo`. In this mode, the workspace is bound server-side to a single configured `OPSMESH_DEMO_WORKSPACE_ID` (default `"default"`), and the unauthenticated endpoints accept that binding explicitly (no browser-supplied `workspace_id` is ever trusted). Rate-limit them (P0-5). Keep `GET /leads`, `GET /approvals`, `GET /runs`, `GET /activity`, `GET /integrations` auth-free in demo mode but scoped to the demo workspace. Document the migration path: when `access_mode=multi_user`, all of these require `Depends(get_current_user)` and bind to `user["workspace_id"]`.
- **Regression tests required:**
  - `test_demo_mode_workspace_is_server_bound` (browser cannot override workspace_id)
  - `test_demo_mode_allows_unauthenticated_read_in_demo_workspace`
  - `test_multi_user_mode_requires_auth` (future path, gated by access_mode)
  - `test_cross_workspace_lead_access_denied_in_multi_user_mode`

### P0-7. Sent emails and lead history are never persisted `[CONFIRMED]`

- **File/function:** `opsmesh/sent_email_store.py:51` `InMemorySentEmailStore` is the only implementation; `app.py:175` always instantiates it. `opsmesh/lead_history_store.py:47` `InMemoryLeadHistoryStore` is the only implementation; `app.py:176` always instantiates it. No DB-backed variants exist. The runtime schema creates `sent_emails` and `lead_history_events` tables (`adapters/db/schema.py`) but no Python store reads from them.
- **Evidence:** grep for `DatabaseSentEmailStore` / `SQLiteSentEmailStore` returns nothing.
- **Impact:** the Activity/Outbox tab and the agent's memory are wiped on every Render redeploy or sleep cycle. The mandate's "Activity and audit history are durable" acceptance criterion fails.
- **Proposed correction (Stage B Phase 4):** implement `DatabaseSentEmailStore` and `DatabaseLeadHistoryStore` reading from the existing `sent_emails` and `lead_history_events` tables. Wire them in `create_app()` when a DB backend is configured; keep the in-memory variants for tests. Verify the schema matches the model fields.
- **Regression tests required:**
  - `test_sent_email_survives_db_restart` (write, reset in-memory state, read from DB)
  - `test_lead_history_survives_db_restart`
  - `test_sent_email_store_workspace_scoped`

### P0-8. CRM outbox + reconcilers are entirely unwired (dead code) `[CONFIRMED]`

- **File/function:**
  - `opsmesh/reconciler/crm_outbox.py:117` `CRMOutboxDrainer` — never constructed in `app.py` or `worker.py`.
  - `opsmesh/reconciler/email_send.py:42` `EmailSendReconciler` — never constructed.
  - `opsmesh/reconciler/approval_timeout.py:37` `ApprovalTimeoutReconciler` — never constructed.
  - `opsmesh/specialists/__init__.py:807` `CRMUpdatingSpecialist` calls the CRM directly, never writes a `CRMOutboxOp`.
- **Evidence:** grep for `EmailSendReconciler(` / `ApprovalTimeoutReconciler(` / `CRMOutboxDrainer(` outside tests returns zero hits.
- **Impact:** `SEND_UNKNOWN` runs never resolve (a timed-out Resend request that was actually accepted server-side is permanently stuck — `run.email_message_id` stays `None` on TimeoutError, `specialists/__init__.py:777`). 24h approval timeouts never fire. The outbox table is never populated. The mandate's "agent workflow genuinely reliable and recoverable" criterion fails.
- **Additional defect:** `EmailSendReconciler.run_once()` (`reconciler/email_send.py:80-81`) directly iterates `self.store._runs.items()` — the private dict of `InMemoryStateStore`. With `SQLiteStateStore` (production), `hasattr(self.store, '_runs')` is False and the reconciler **silently does nothing** even if wired.
- **Proposed correction (Stage B Phase 3):**
  1. Make `CRMUpdatingSpecialist` write a `CRMOutboxOp` (status `PENDING`) to the outbox store instead of calling the CRM directly; the drainer then drains it.
  2. Instantiate all three reconcilers in `worker.py` (and in `build_in_process_supervisor()` for the API's in-process worker). Run them on a polling loop alongside the job claim loop.
  3. Fix `EmailSendReconciler` to use a proper `StateStore.list_in_state(State.SEND_UNKNOWN)` method instead of poking `_runs`.
  4. Fix `SendingSpecialist` to persist `email_message_id` even on TimeoutError (Resend may have accepted the request server-side; the reconciler needs the ID to poll `get_status`).
- **Regression tests required:**
  - `test_send_unknown_resolves_to_sent_after_reconciler_poll` (mock provider returns `confirmed_sent` on retry)
  - `test_send_unknown_resolves_to_failed_after_max_attempts`
  - `test_send_unknown_with_message_id_polled_correctly`
  - `test_crm_outbox_drains_pending_op_to_crm`
  - `test_crm_outbox_retries_on_transient_failure`
  - `test_crm_outbox_dead_letters_after_max_attempts`
  - `test_approval_timeout_expires_after_24h`
  - `test_reconciler_works_with_sqlite_state_store` (the `_runs` private-attr bug)

---

## P1 — Serious security or functional defects

### P1-1. `render.yaml` CORS wildcard with credentials `[CONFIRMED]`
- **File/function:** `render.yaml:58-59` `OPSMESH_CORS_ALLOWED_ORIGINS="*"`; `app.py:301-307` `allow_credentials=True`.
- **Impact:** wildcard + credentials is a browser-rejected combination, but the intent is "allow all" and any non-credentialed path is wide open. The frontend stores the session JWT in `localStorage` (`js/app.js:14-25`) — XSS exposes 7-day tokens.
- **Correction (Stage B Phase 1):** set `OPSMESH_CORS_ALLOWED_ORIGINS` to the API's own origin (the frontend is served same-origin at `/ui`); after OAuth removal, credentials are not needed. Read CORS from `Settings` instead of `os.environ.get` directly.
- **Tests:** `test_cors_rejects_unknown_origin`, `test_cors_no_credentials_after_oauth_removal`.

### P1-2. Hardcoded sender `from_="bekalu@opsmesh.dev"` in Gate 2 approval payload `[CONFIRMED]`
- **File/function:** `opsmesh/supervisor.py:550` hardcodes `from_="bekalu@opsmesh.dev"`. `opsmesh/specialists/__init__.py:709` reads `settings.workspace_sender_identity` for the actual send.
- **Impact:** the approval payload hash binds to the wrong `from_`. If ToolGateway were wired in, the payload-hash verification would reject legitimately-approved sends (or worse, an attacker controlling the approval payload could make a wrong hash match). This is the root cause of the user's Resend 403 — the unverified `opsmesh.dev` domain is hardcoded in two places.
- **Correction (Stage B Phase 2):** read `settings.workspace_sender_identity` in `_create_gate2_approval` and pass it through to the payload. Default the setting to `OpsMesh <onboarding@resend.dev>` for demo. Add `OPSMESH_WORKSPACE_SENDER_IDENTITY` to `render.yaml`.
- **Tests:** `test_gate2_payload_uses_configured_sender_identity`, `test_payload_hash_matches_send_identity`.

### P1-3. Frontend lead-detail view is missing (table rows not clickable) `[CONFIRMED]`
- **File/function:** `opsmesh/static/leads.html:131` — no `onclick`, no link. `GET /api/v1/leads/{lead_id}` exists (`api/routes/leads.py:217`) but is never called by the UI. `runs.html:168` links the lead ID back to the list, not a detail.
- **Impact:** the mandate's "lead detail view feel like a real sales intelligence workspace" is unmet. Operators cannot see qualification breakdown, evidence, or research from a lead context.
- **Correction (Stage B Phase 7 — frontend):** add a lead-detail branch to `leads.html` (like `runs.html:22-28`), render company/contact info, qualification score breakdown, evidence with source links, research findings, run history, CRM sync status. Make table rows clickable.
- **Tests:** frontend smoke test (Playwright or manual) — click a lead row, assert detail renders.

### P1-4. Settings / Operations page is entirely missing `[CONFIRMED]`
- **File/function:** `opsmesh/static/js/shell.js:8-15` `NAV_ITEMS` has 6 entries (Overview, Leads, Runs, Approvals, Activity, Integrations) — no Settings. README references `/ui/settings.html` (`README.md:159`) but the file does not exist.
- **Impact:** operators cannot toggle `email_kill_switch`, manage webhook/Tally secrets, view audit log, pause/resume the worker, or see system config validation. The mandate's "Settings / Operations" nav item is unmet.
- **Correction (Stage B Phase 7):** add a `settings.html` page with read-only config validation display, `email_kill_switch` toggle (gated by demo-mode policy), audit log viewer, worker status, and demo-data reset (guarded).
- **Tests:** smoke test that settings page loads and surfaces config validation.

### P1-5. Integrations page hides vendor names, missing Tally + Worker status `[CONFIRMED]`
- **File/function:** `opsmesh/static/integrations.html:72-77` shows abstract "LLM/Research/Email/CRM" roles; vendor name buried in `p.provider` (`:93`). No Tally card. No Worker status endpoint or card.
- **Impact:** the mandate requires "show the actual status of Groq, Exa, Resend, HubSpot, Tally, Database, Worker" — vendor names and two integrations are missing.
- **Correction (Stage B Phase 7):** rename cards to vendor names; add Tally webhook status card (configured/connected/degraded/untested); add a worker status endpoint (`GET /api/v1/worker/status` returning last-claim timestamp, processed/failed counts, queue depth) and a card.
- **Tests:** integration test that the endpoint returns the right shape; smoke test the card renders.

### P1-6. Logout is client-side only; JWT remains valid 7 days `[CONFIRMED]`
- **File/function:** `opsmesh/static/js/app.js:116-122` `auth.logout()` clears localStorage, no server revocation. `auth_google.py:89` session JWT exp 7 days. No revocation endpoint.
- **Impact:** "sign out" does not sign out — a stolen token is valid for 7 days. (Moot for the demo after OAuth removal, but the session-secret path must be cleaned up.)
- **Correction (Stage B Phase 5 — OAuth removal):** with OAuth removed, the session JWT is no longer issued. The `single_user_demo` mode has no login. Document that there is no server-side session to revoke in demo mode.

### P1-7. `SQLiteApprovalStore` / `SQLiteOutboxStore` mix synchronous `sqlite3` into async code `[CONFIRMED]`
- **File/function:** `opsmesh/adapters/db/sqlite_approval_store.py:82, 116, 340, 388` and `opsmesh/adapters/db/sqlite_outbox_store.py:48, 80, 150, 167, 184, 201, 230, 249, 268` — `create`/`get`/`list_pending`/`find_expired_pending`/`mark_done`/`mark_failed`/`requeue`/`enqueue` use synchronous `sqlite3.connect()` inside async functions.
- **Impact:** every call blocks the event loop — DoS vector under load and a correctness hazard with WAL mode. Other stores use `aiosqlite` correctly.
- **Correction (Stage B Phase 4):** migrate the sync calls to `aiosqlite` (the pattern is already established in `SQLiteStateStore` / `SQLiteJobQueue`).
- **Tests:** `test_approval_store_does_not_block_event_loop` (assert call completes within a bounded time under contention), existing approval tests still pass.

### P1-8. Activity is split into two tabs, not a unified chronological feed `[CONFIRMED]`
- **File/function:** `opsmesh/static/activity.html:156-159` two tabs (Sent Emails / History); `GET /api/v1/activity/leads/{lead_id}` (`api/routes/activity.py:109`) is unused.
- **Impact:** the mandate requires "a searchable chronological activity feed" with filters by event type, run, lead, status. The current split siloes emails from state transitions.
- **Correction (Stage B Phase 7):** unify into a single feed with type/run/lead/status filters; surface the per-lead activity endpoint in the lead detail view.
- **Tests:** smoke test filters work; backend test that the unified endpoint returns events sorted by timestamp.

---

## P2 — Hardening gaps and correctness hazards

### P2-1. Module-level mutable singletons in every route file `[CONFIRMED]`
- **File/function:** `_store`, `_intake_service`, `_workspace_id`, `_queue`, `_gate`, `_sent_email_store`, `_history_store`, `_secret`, `_tally_secret` at module level in every `api/routes/*.py`. `configure_*` functions do `global _x` assignment.
- **Impact:** `create_app()` is not safe to call twice in one process — the second app overwrites the first's singletons. Tests must `reset_db_state()` or leak. Concurrent FastAPI apps in one process cross-contaminate.
- **Correction (Stage B Phase 4):** move singletons onto `app.state` (FastAPI's per-app state container) or into a `Runtime` object passed via `Depends`. Keep `configure_*` for back-compat but have them write to `app.state`.
- **Tests:** `test_create_app_idempotent_in_process`, `test_two_apps_in_process_isolated`.

### P2-2. `EmailSendReconciler` pokes `InMemoryStateStore._runs` directly `[CONFIRMED]`
- **File/function:** `opsmesh/reconciler/email_send.py:80-81` — `hasattr(self.store, '_runs')` then iterates `self.store._runs.items()`.
- **Impact:** with `SQLiteStateStore`, the reconciler silently no-ops (the attribute doesn't exist).
- **Correction (Stage B Phase 3):** add a `StateStore.list_in_state(state)` method to the ABC; implement in both stores; use it in the reconciler.
- **Tests:** `test_email_reconciler_works_with_sqlite_state_store`, `test_email_reconciler_works_with_in_memory_state_store`.

### P2-3. `WEBHOOK_SECRET` (unprefixed) fallback in code `[CONFIRMED]`
- **File/function:** `opsmesh/webhook.py:95` `os.environ.get("WEBHOOK_SECRET", "")` and docstrings at `:18, 49, 82` reference `WEBHOOK_SECRET`. The configured name is `OPSMESH_WEBHOOK_SECRET`.
- **Impact:** the fallback always returns `""`. The route handler passes `_secret` explicitly so it works, but anyone calling `verify_hmac()` without the secret arg gets a silent fail-closed. Misleading docs.
- **Correction (Stage B Phase 1):** remove the unprefixed fallback; read from `Settings.webhook_secret` only. Fix the docstrings.
- **Tests:** `test_verify_hmac_without_secret_raises`.

### P2-4. `OPSMESH_EMAIL_FROM` documented but no Settings field `[CONFIRMED]`
- **File/function:** `.env.example:34` documents `# OPSMESH_EMAIL_FROM=you@yourdomain.com`; `config.py` has no `email_from` field. The real knob is `OPSMESH_WORKSPACE_SENDER_IDENTITY` (`config.py:130`).
- **Impact:** operators set `OPSMESH_EMAIL_FROM` and nothing happens. Misleading.
- **Correction (Stage B Phase 1):** remove the `OPSMESH_EMAIL_FROM` line from `.env.example`; document `OPSMESH_WORKSPACE_SENDER_IDENTITY` clearly with the `onboarding@resend.dev` demo default.
- **Tests:** config validation test that `workspace_sender_identity` is the source of truth.

### P2-5. `render.yaml` worker missing `OPSMESH_PRODUCTION_MODE` `[CONFIRMED]`
- **File/function:** `render.yaml` worker block lacks `OPSMESH_PRODUCTION_MODE=true`.
- **Impact:** worker does not enforce prod-mode startup checks (fail-closed on missing/`change-me` webhook secret).
- **Correction (Stage B Phase 1):** add `OPSMESH_PRODUCTION_MODE=true` to the worker env block.
- **Tests:** startup test in prod mode.

### P2-6. Exception-text echo in auth + provider errors `[CONFIRMED]`
- **File/function:** `opsmesh/api/auth_google.py:161, 172, 177` `f"... {str(e)[:200]}"`; `opsmesh/api/auth.py:161`; specialists log `f"... {type(e).__name__}: {e}"` at `specialists/__init__.py:163, 210, 247, 301, 423, 578, 677, 782, 816`; provider exceptions embed `response.text[:200]` (`groq.py:140-142`, `resend.py:108-110`, `hubspot.py:135-138, 184-187`). `last_error` returned verbatim via `/runs` and `/runs/{id}/timeline`.
- **Impact:** potential credential leakage in error responses (PyJWT/httpx errors can echo URLs, header bytes).
- **Correction (Stage B Phase 5):** with OAuth removal, the auth error paths go away. For provider errors, scrub `response.text` before storing in `last_error`; keep the full text in server logs only. Add a `sanitized_error` helper.
- **Tests:** `test_last_error_does_not_contain_provider_response_body`.

### P2-7. No ARIA roles on Activity tab bar; borderline WCAG contrast `[CONFIRMED]`
- **File/function:** `opsmesh/static/activity.html:156-159` plain `<div class="tab">`; `opsmesh/static/css/app.css:13` `--text-mute #6b7280` on `--surface #171a21` ~4.0:1 (borderline AA fail).
- **Impact:** accessibility gap; mandate requires "accessible contrast and keyboard navigation."
- **Correction (Stage B Phase 7):** use `role="tablist"`/`role="tab"` with `aria-selected`; darken `--text-mute` to ≥ 4.5:1; add skip-to-content link; `aria-live` on toasts.
- **Tests:** axe-core or manual a11y audit.

### P2-8. No chart / trend visualization on dashboard `[CONFIRMED]`
- **File/function:** `opsmesh/static/dashboard.html` is tables + numeric tiles only; no time-series for cost, success rate, run volume, queue depth despite metrics existing in the API payload.
- **Impact:** mandate requires "charts based on real API data" with date-range filtering. Dashboard is not "genuinely useful to an operator."
- **Correction (Stage B Phase 7):** add a lightweight chart (inline SVG or a tiny chart lib) for runs-over-time, success-rate-over-time, cost-over-time; add date-range filter. Avoid "fake charts" — bind to real `/api/v1/dashboard?range=7d` data.
- **Tests:** smoke test the chart renders with real data; empty-state when no data.

### P2-9. Reconciler poll loop not wired; no `/api/v1/worker/status` `[CONFIRMED]`
- **File/function:** no worker health endpoint; reconcilers never started.
- **Impact:** observability gap; mandate requires worker status on Integrations page.
- **Correction (Stage B Phase 3):** start reconcilers in worker; add `/api/v1/worker/status` endpoint; surface on Integrations.
- **Tests:** endpoint test; reconciler-start test.

### P2-10. No iconography; letter-based event icons; hyphen empty-state `[CONFIRMED]`
- **File/function:** `activity.html:245-248` single-letter event icons; `app.js:299` `.empty .icon` content `"-"`; `shell.js:30` brand mark is `*`.
- **Impact:** reads as unfinished, not premium B2B.
- **Correction (Stage B Phase 7):** adopt a small inline-SVG icon set (e.g. Lucide, MIT-licensed) for nav, events, empty states, buttons.
- **Tests:** visual check.

---

## P3 — Hygiene, drift, documentation

### P3-1. `OPSMESH_GOOGLE_OAUTH_CLIENT_SECRET` declared but never read `[CONFIRMED]`
- **File/function:** `config.py:118`, `.env.example:51`. `auth_google.py` only uses `google_oauth_client_id`.
- **Correction:** remove with OAuth removal (Stage B Phase 5).

### P3-2. `OPSMESH_HUBSPOT_API_KEY` mentioned in `hubspot.py:12` docstring but real name is `OPSMESH_CRM_API_KEY` `[CONFIRMED]`
- **Correction:** fix the docstring.

### P3-3. `OPSMESH_OBS_BACKEND` / `OPSMESH_OTEL_EXPORTER_OTLP_ENDPOINT` not set in `render.yaml` `[CONFIRMED]`
- **Correction:** set to `local` for demo; document OTLP path for future prod.

### P3-4. `OPSMESH_WORKER_IN_PROCESS` undocumented `[CONFIRMED]`
- **File/function:** `config.py:77` default `"auto"`; not in `render.yaml` or `.env.example`.
- **Correction:** document in `.env.example`.

### P3-5. `sqlalchemy==2.1.8` invalid pin in `requirements.txt` `[CONFIRMED]`
- See `DEPENDENCY_MAP.md` §3.

### P3-6. `google-generativeai==0.8.3` wrong-provider dependency `[CONFIRMED]`
- See `DEPENDENCY_MAP.md` §4.

### P3-7. Dev dependencies (`ruff`, `mypy`) not declared `[CONFIRMED]`
- See `DEPENDENCY_MAP.md` §6.

### P3-8. README references `/ui/settings.html` which does not exist `[CONFIRMED]`
- **File/function:** `README.md:159`.
- **Correction:** add the page (P1-4) or fix the README.

### P3-9. Dead CSS (`.skeleton`, `.empty-state`) defined but never used `[CONFIRMED]`
- **File/function:** `app.css:651-660`, `app.css:630-648`.
- **Correction:** remove or use them.

### P3-10. `[] + stat(...)` coercion hack in dashboard `[CONFIRMED]`
- **File/function:** `dashboard.html:71`.
- **Correction:** replace with proper string concatenation.

### P3-11. Only 1 Alembic migration; runtime DDL drifts from it `[CONFIRMED]`
- See `DEPLOYMENT_BASELINE.md` §7.

### P3-12. `require_workspace` helper declared but never called `[CONFIRMED]`
- **File/function:** `opsmesh/api/auth.py:165-176`.
- **Correction:** use it in routes that need workspace binding, or remove.

---

## Suspected risks (require verification)

- `[SUSPECTED]` **Worker free-tier sleep interaction with in-process worker.** When `OPSMESH_WORKER_IN_PROCESS=auto` resolves to True (single-process dev), the API process runs the worker. On Render, the API sleeps after 15 min inactivity — does the in-process worker also sleep? Verify and document. (Likely fine: render.yaml uses the separate worker service.)
- `[SUSPECTED]` **Postgres free-tier connection limits under load.** `asyncpg` pool size not configured. Verify the default pool doesn't exhaust under concurrent webhook bursts.
- `[SUSPECTED]` **`SCHEMA_POSTGRES` vs `SCHEMA_SQLITE` drift.** Both are in `adapters/db/schema.py`. Verify they create identical table sets; the audit found `workspace_members` added to one path. A migration test should diff them.

---

## Optional enhancements (defer unless justified)

These map to the mandate's "PRODUCT ENHANCEMENTS — ADD ONLY WHEN JUSTIFIED" list. Each is scored for business value vs. implementation cost; see `STAGE_A_PLAN.md` Phase 8 for the include/defer decision per item.

1. Configurable ICP and qualification rules — **INCLUDE** (mandate requires "configurable without changing source code").
2. Evidence quality and source freshness indicators — INCLUDE.
3. Company and contact deduplication — INCLUDE (dedup index exists but is in-memory; persist it).
4. Buying-signal detection — DEFER (requires reliable data source; not justified for demo).
5. Lead prioritization — INCLUDE (cheap; sort by score).
6. Research refresh — DEFER (cost; not justified for demo).
7. Human-requested additional research — INCLUDE (Gate 1 already supports `request_more_info`; wire the UI).
8. Email templates and tone configuration — DEFER (scope creep).
9. Draft version history — INCLUDE (cheap; persist draft snapshots).
10. Suppression and unsubscribe management — INCLUDE (mandate requires; persist the list).
11. CRM field mapping — DEFER (HubSpot adapter is sufficient).
12. CRM sync conflict detection — DEFER.
13. Manual retry and recovery controls — INCLUDE (DLQ retry button).
14. Exportable run reports — DEFER.
15. Operational health checks — INCLUDE (worker status endpoint).
16. Configurable cost and budget limits — INCLUDE (already in Settings; surface in UI).
17. Evaluation reports — INCLUDE (eval harness exists; expand to 30 scenarios).
18. Demo data seeding and reset — INCLUDE (mandate requires for demo).
19. Audit history — INCLUDE (audit_events table exists; wire it).
20. System configuration validation — INCLUDE (Settings validation on boot).

**Explicitly NOT built (per mandate):** unnecessary autonomous agents, unbounded browser agents, generic chat interface, vector databases, complex microservices, artificial features for code volume.

---

## Finding count summary

| Severity | Count |
|---|---|
| P0 | 8 |
| P1 | 8 |
| P2 | 10 |
| P3 | 12 |
| Suspected | 3 |
| Optional enhancements evaluated | 20 (12 include, 8 defer) |
| **Total confirmed findings** | **38** |

**No P0 may remain unresolved at Stage C acceptance.** The Stage B plan in `STAGE_A_PLAN.md` addresses every P0 and P1, plus the P2/P3 items that block the mandate's acceptance criteria.
