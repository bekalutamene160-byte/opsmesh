# OPSMESH Architecture Corrections

**Status:** Authoritative amendment
**Date:** 2026-10-31
**Precedence:** This document supersedes conflicting statements in the original `OPSMESH_Architecture_Package.pdf`. The original PDF is preserved as the baseline; this amendment takes precedence where they conflict.

---

## Purpose

The original architecture package contains several internal contradictions and unsafe defaults. This document corrects them before implementation begins. Each correction follows this format:

- **Original decision** — what the PDF said
- **Problem found** — why it's wrong
- **Corrected decision** — what we do instead
- **Reason** — the engineering justification
- **Affected documents** — which sections of the architecture are impacted
- **Affected implementation** — which code components change
- **Tests required** — what tests enforce the correction

---

## Correction 1: State count is 16, not 13

- **Original:** §06.1 prose says "13 states"; the table below lists 16.
- **Problem:** Internal contradiction. The prose is a typo.
- **Corrected:** 16 states is correct. All documentation must say 16.
- **Reason:** The table is the authoritative enumeration. Count: NEW, RESEARCHING, AWAITING_EVIDENCE_REVIEW, EVIDENCE_PROVIDED, QUALIFYING, DRAFTING, CRITIC, AWAITING_APPROVAL, SENDING, SEND_UNKNOWN, CRM_UPDATING, SENT, REJECTED, FAILED, STOPPED_BY_USER, APPROVAL_EXPIRED = 16.
- **Affected documents:** §06 (Workflow State Machine), acceptance-checklist, §14 (Roadmap Day 1).
- **Affected implementation:** `state_machine.py` — pinned at 16 in `test_state_count_is_16`.
- **Tests required:** `test_state_count_is_16`, `test_all_expected_states_present`.

---

## Correction 2: SENT does NOT mean CRM sync is complete

- **Original:** §06 and §10 conflated SENT with "CRM updated."
- **Problem:** This creates a false guarantee. The email provider may confirm sending, but the CRM may be unavailable. If SENT implied CRM sync complete, a CRM outage would block the run from reaching a terminal state.
- **Corrected:** SENT means:
  1. The email provider has definitively confirmed acceptance/sending of the exact approved message.
  2. The CRM outbox operation has been durably committed (the outbox row exists with status=PENDING).
  3. The run can close independently of remote CRM synchronization.
  SENT does NOT mean the external CRM has completed synchronization. The outbox operation has its own independent lifecycle.
- **Reason:** Decouples email success from CRM availability. A CRM failure after email send must never block the run from reaching terminal state, and must never trigger an email resend.
- **Affected documents:** §06 (state definitions), §10 (Reliability), §07 (Data Model — crm_outbox), §08 (API contracts), §15 (Demo script).
- **Affected implementation:** `models.py` — add `CRMOutboxStatus` enum and `CRMOutboxOp` model. `state_machine.py` — CRM_UPDATING → SENT transition is unconditional (the outbox commit is what matters, not the CRM sync).
- **Tests required:** `test_crm_outbox_status_enum`, `test_sent_does_not_require_crm_sync`.

---

## Correction 3: SEND_UNKNOWN never auto-resolves to FAILED

- **Original:** `ALLOWED_TRANSITIONS[SEND_UNKNOWN] = {SENT, FAILED}` with no constraint on who can transition.
- **Problem:** This permitted the supervisor or a naive reconciler to auto-fail a run after a timeout threshold, which could discard a successfully-sent email.
- **Corrected:** The transitions SEND_UNKNOWN → SENT and SEND_UNKNOWN → FAILED remain legal, BUT only the reconciler may execute them, and ONLY on conclusive evidence:
  - SEND_UNKNOWN → SENT: provider confirms the message was sent.
  - SEND_UNKNOWN → FAILED: provider confirms the message was NOT sent.
  If the outcome remains unknown after 5 reconciliation attempts, the run stays in SEND_UNKNOWN and is escalated for manual investigation. NEVER auto-transition to FAILED due to threshold exhaustion.
- **Reason:** Exactly-once delivery across external providers is impossible. The safest behavior is to freeze the run and alert the operator rather than risk a duplicate send or a lost send.
- **Affected documents:** §06 (state machine), §10 (Reliability — §4 Reconciliation), §09 (Security — residual risks).
- **Affected implementation:** `state_machine.py` — transitions remain but documented as reconciler-only. Day 5 reconciler enforces the conclusive-evidence rule.
- **Tests required:** `test_send_unknown_to_sent_allowed`, `test_send_unknown_to_failed_allowed`, Day 5: `test_reconciler_does_not_auto_fail_after_threshold`.

---

## Correction 4: FAILED is truly terminal — no FAILED → RESEARCHING

- **Original:** `ALLOWED_TRANSITIONS[FAILED] = {RESEARCHING}` — permitted retry on the same run_id.
- **Problem:** This contradicts FAILED being terminal. Reviving a failed run erases the failure history and risks re-using stale idempotency keys. It also makes the state machine non-deterministic: a run that "failed" might silently restart.
- **Corrected:** `ALLOWED_TRANSITIONS[FAILED] = set()` — truly terminal. Operator-initiated retry creates a NEW run with a `parent_run_id` field linking to the original failed run. The original run's history is preserved immutably.
- **Reason:** Terminal means terminal. A retry is a new workflow attempt, not a continuation of the failed one. This preserves audit trail integrity and idempotency-key uniqueness.
- **Affected documents:** §06 (state machine), §07 (Data Model — add parent_run_id), §10 (Recovery), §13 (Runbook — DLQ replay).
- **Affected implementation:** `state_machine.py` — `FAILED: set()`. `models.py` — add `parent_run_id: Optional[str]` to RunRecord.
- **Tests required:** `test_failed_is_truly_terminal`, `test_failed_has_no_outgoing_transitions`.

---

## Correction 5: Gate 1 never authorizes sending

- **Original:** §06 §5 described Gate 1's "proceed_with_limitations" decision but did not explicitly forbid it from authorizing email send.
- **Problem:** Ambiguity could lead to a bug where "proceed" is treated as approval to send.
- **Corrected:** Gate 1 has exactly three decisions: `proceed_with_limitations`, `request_more_info`, `stop`. None of these authorizes email send. Gate 1 transitions the run to DRAFTING (proceed) or EVIDENCE_PROVIDED (request_more_info) or STOPPED_BY_USER (stop). Email send requires a separate Gate 2 approval with exact payload binding. Additionally, "proceed_with_limitations" does NOT permit ungrounded claims — the Draft specialist must still use only verified evidence and disclose uncertainty.
- **Reason:** Separation of concerns. Gate 1 is about evidence sufficiency; Gate 2 is about message authorization. Conflating them creates a security hole.
- **Affected documents:** §06 (§5 Gate 1), §05 (Agent Design — Draft specialist prompt), §09 (Security).
- **Affected implementation:** `supervisor.py` — _apply_result for research: if insufficient, transition to AWAITING_EVIDENCE_REVIEW (not DRAFTING). Day 5: ApprovalGate enforces Gate 1 decisions independently from Gate 2.
- **Tests required:** `test_gate1_does_not_authorize_sending`, `test_insufficient_evidence_pauses_at_gate1`.

---

## Correction 6: Lead disposition is a first-class concept

- **Original:** The architecture mentioned lead status but did not define a complete disposition model.
- **Problem:** Without a clear disposition model, "contacted" could be set when the email wasn't confirmed sent, or "qualified" could be overwritten by Gate 1.
- **Corrected:** Define a `LeadDisposition` enum with values: NEW, RESEARCHING, QUALIFIED, DISQUALIFIED, REJECTED, CONTACTED, STOPPED, NEEDS_REVIEW, APPROVAL_EXPIRED, FAILED. Rules:
  - Never mark CONTACTED unless email send is definitively confirmed (SENT state reached).
  - DISQUALIFIED is never overwritten by Gate 1 "proceed."
  - Every terminal run state maps to a lead disposition update.
  - Internal OPSMESH lead record is always maintained, even when external CRM sync is pending or failed.
- **Reason:** The internal lead ledger is the source of truth for lead outcomes. The external CRM is a downstream sync target.
- **Affected documents:** §07 (Data Model — leads table), §05 (Agent Design), §10 (Reliability).
- **Affected implementation:** `models.py` — add `LeadDisposition` enum, `Lead` model.
- **Tests required:** `test_lead_disposition_enum`, `test_contacted_only_after_sent`.

---

## Correction 7: Contact-policy safeguards

- **Original:** Not present in the architecture.
- **Problem:** The MVP restricts to inbound leads, but without explicit contact-policy enforcement, the system could be misused for scraped-list cold outreach.
- **Corrected:** Add contact-policy model with:
  - Lead source tracking (manual, webhook, simulated — no "scraped").
  - Suppression list (persistent, checked pre-send).
  - Opt-out tracking.
  - Clear sender identity.
  - Audit trail of contact decisions.
  - Pre-send suppression check is mandatory and cannot be bypassed by human approval.
- **Reason:** Human approval does not override contact policy. A suppressed email must never be sent, even if the operator clicks "Approve."
- **Affected documents:** §01 (Product Scope — non-goals), §03 (Engineering Contract), §09 (Security).
- **Affected implementation:** `models.py` — add `ContactPolicy` model. Day 4: ToolGateway enforces pre-send suppression check. Day 5: send_email tool checks suppression list before dispatch.
- **Tests required:** `test_contact_policy_model`, Day 4: `test_suppressed_email_rejected`.

---

## Correction 8: Workspace isolation is a mandatory invariant

- **Original:** Mentioned but not enforced at every layer.
- **Problem:** "Workspace-scoped queries" is easy to say but easy to forget in practice.
- **Corrected:** Workspace isolation is mandatory at every layer: API, auth, database queries, StateStore, JobQueue, ApprovalService, ToolGateway, MCP servers, CRM adapters, reconciler, audit records. The non-goal is enterprise tenant administration (SSO, role hierarchies); the goal is basic cross-workspace data isolation.
- **Reason:** A bug in one query that omits `workspace_id` could leak data across workspaces. Defense in depth.
- **Affected documents:** §04 (System Architecture), §07 (Data Model), §09 (Security).
- **Affected implementation:** `state_store.py` — `load_latest(run_id, workspace_id)` enforces scoping; returns None for cross-workspace access. Day 2+: every query includes workspace_id.
- **Tests required:** `test_cross_workspace_access_returns_none`, `test_workspace_scoped_list`.

---

## Correction 9: Tool counting is explicit

- **Original:** Ambiguous — sometimes "5 tools," sometimes "6 tools."
- **Problem:** web_search was sometimes counted as an MCP tool, sometimes not.
- **Corrected:** 5 MCP tools across 2 MCP servers (CRM: lookup_lead, update_lead_status, create_lead; Comms: send_email, send_notification). web_search is an inline research capability, NOT a sixth MCP tool.
- **Reason:** web_search has no credentials to protect (when using a free search provider) and lives inside the Research specialist. MCP is for tools that need an auth boundary.
- **Affected documents:** §05 (Agent and Tool Design), ADR-005.
- **Affected implementation:** No Day 1 impact. Day 4: MCP servers expose 5 tools; web_search is inline.
- **Tests required:** Day 4 contract tests.

---

## Correction 10: Evaluation benchmark is exactly 24 scenarios

- **Original:** Conflicting statements: "24 scenarios," "20 deterministic + 4 LLM-judged," "6 adversarial," "7 failure-recovery."
- **Problem:** The counts didn't add up consistently.
- **Corrected:** Exactly 24 stable benchmark scenarios in one authoritative manifest. Classification:
  - 20 deterministic scenarios (assertions on state, permissions, idempotency, schema).
  - 4 LLM-judged scenarios (qualitative critique quality only).
  - 6 adversarial scenarios (subset of the 24, overlapping with deterministic).
  - 7 failure-recovery scenarios (subset of the 24, overlapping with deterministic).
  - Release thresholds: ≥90% pass on 20 deterministic; 100% on 6 adversarial; 100% on 7 failure-recovery.
- **Reason:** One manifest, consistent classification, no ambiguity.
- **Affected documents:** §11 (Evaluation and Testing), §14 (Roadmap Day 7), CI instructions.
- **Affected implementation:** Day 7: `eval/scenarios.jsonl` with exactly 24 entries.
- **Tests required:** Day 7: `test_eval_scenario_count_is_24`.

---

## Correction 11: Workflow step definitions are explicit

- **Original:** "9-step workflow" vs. supervisor PLAN with 7 items — ambiguous.
- **Problem:** Different documents counted different things as "steps."
- **Corrected:** Define exactly:
  - **Business workflow stage:** research, qualify, draft, critique, approve, send, sync, close (8 stages).
  - **Specialist execution step:** a single specialist invocation (research, qualify, draft, critic — 4 specialists).
  - **State transition:** a move in the state machine (16 states, ~30 legal transitions).
  - **Human approval pause:** Gate 1 (AWAITING_EVIDENCE_REVIEW) or Gate 2 (AWAITING_APPROVAL).
  - **Background reconciliation:** SEND_UNKNOWN reconciler, CRM outbox drainer, approval-timeout reconciler.
  - **Run closure:** terminal state reached; metrics recorded; lead disposition updated.
- **Reason:** Consistent terminology prevents implementation drift.
- **Affected documents:** §06, §14, §05.
- **Affected implementation:** `supervisor.py` — PLAN lists specialist steps only; gates and reconciliation are states/operations, not plan steps.
- **Tests required:** Documentation consistency (manual review).

---

## Correction 12: Seven-day schedule, full scope

- **Original:** 14-day course-aligned roadmap.
- **Problem:** Developer chose 7-day compression; original roadmap didn't reflect this.
- **Corrected:** 7-day schedule that compresses calendar time without cutting scope. Each day combines ~2 original days around a coherent subsystem. All 16 states, all 5 MCP tools, both gates, all 24 eval scenarios, all guardrails, deployment, and demo still ship.
- **Reason:** Compression changes the schedule, not the architecture. Incomplete work carries forward.
- **Affected documents:** §14 (Implementation Roadmap).
- **Affected implementation:** No code impact; schedule only.
- **Tests required:** All acceptance-gate tests still required by end of Day 7.

---

## Correction 13: LLM provider decision deferred

- **Original:** Proposed Groq (Llama 3.3 70B) as primary, Gemini as fallback.
- **Problem:** This is not yet an approved architectural change. The provider must be evaluated against: SDK support, structured output, rate limits, latency, quotas, timeout handling, fallback behavior.
- **Corrected:** Preserve the LLM provider abstraction (ADR-002). Mock provider remains the default for deterministic tests and the reproducible demo. The Groq-vs-Gemini decision is deferred until Day 2 when we implement the real adapter and can evaluate concretely.
- **Reason:** Don't change architecture without evidence. Mock-first keeps CI hermetic regardless of the real provider choice.
- **Affected documents:** ADR-002.
- **Affected implementation:** Day 1: no LLM calls (mock specialists only). Day 2: implement adapter behind the Protocol; evaluate Groq then.
- **Tests required:** Day 2 adapter contract tests.

---

## Correction 14: Persistence uses monotonic versioning, not just step_id

- **Original:** Optimistic concurrency on step_id only.
- **Problem:** step_id is an in-memory counter; it doesn't protect against concurrent production writes robustly. A dedicated version field with compare-and-swap semantics is needed.
- **Corrected:** Add `version: int` field to RunRecord (starts at 0, increments on every save). StateStore.save_checkpoint takes `expected_version: int` — if the persisted version doesn't match, raise ConcurrentModification. This is atomic at the DB level (UPDATE ... WHERE version = $expected).
- **Reason:** Database-enforced compare-and-swap is the only safe way to handle concurrent writes. step_id is for human/audit readability; version is for concurrency control.
- **Affected documents:** §07 (Data Model), §10 (Reliability — §6 Crash recovery).
- **Affected implementation:** `models.py` — add `version` field. `state_store.py` — save_checkpoint takes expected_version, returns new version.
- **Tests required:** `test_monotonic_versioning`, `test_stale_version_rejected`, `test_optimistic_concurrency`.

---

## Correction 15: StateStore is an ABC, not a Protocol

- **Original:** Mixed terminology — code used ABC but docs called it Protocol.
- **Problem:** Confusing. Python's `abc.ABC` and `typing.Protocol` are different mechanisms.
- **Corrected:** StateStore is an abstract base class (`abc.ABC`). Documented as such. No Protocol terminology.
- **Reason:** Clarity. ABC is the right choice here because we want a shared interface with default behavior patterns and explicit inheritance.
- **Affected documents:** §05 (Agent Design — adapter interfaces), §07 (Data Model).
- **Affected implementation:** `state_store.py` — `class StateStore(ABC)`.
- **Tests required:** Existing tests still pass.

---

## Correction 16: Supervisor uses dynamic step computation, not a stale precomputed plan

- **Original:** Supervisor computed `_remaining_steps` once, then iterated with a for loop.
- **Problem:** If the Critic rejected a draft, the supervisor transitioned back to DRAFTING, but the precomputed remaining steps still contained the old "critic" and "sending" entries — leading to stale execution.
- **Corrected:** Supervisor uses a while loop. Each iteration calls `_next_step(run)` which recomputes based on the CURRENT state. After a Critic rejection, the state is DRAFTING, so `_next_step` returns "drafting" — not the stale "critic" from the old plan.
- **Reason:** Dynamic control flow prevents stale-plan bugs. The supervisor reacts to the current state, not to a plan computed before the state changed.
- **Affected documents:** §05 (Agent Design — Supervisor), §06 (state machine).
- **Affected implementation:** `supervisor.py` — while loop + `_next_step`.
- **Tests required:** `test_critic_rejection_retries_drafting`, `test_max_draft_attempts_enforced`, `test_resume_does_not_repeat_completed_specialists`.

---

## Correction 17: Unknown Critic verdict fails closed

- **Original:** _apply_result for critic only checked APPROVED and REJECTED. An unknown verdict was treated as approval (fell through to the AWAITING_APPROVAL transition).
- **Problem:** A malformed critic result (e.g., typo in verdict string) would silently authorize email send. This is a security hole.
- **Corrected:** _apply_result explicitly handles APPROVED → AWAITING_APPROVAL, REJECTED → DRAFTING (retry), and ANY OTHER verdict → FAILED (fail closed).
- **Reason:** Never treat an unknown result as approval. Fail loud, fail closed.
- **Affected documents:** §05 (Agent Design — Critic), §09 (Security).
- **Affected implementation:** `supervisor.py` — _apply_result for critic step.
- **Tests required:** `test_unknown_critic_verdict_fails_closed`.

---

## Correction 18: Exception messages are sanitized

- **Original:** `run.last_error = f"{type(e).__name__}: {e}"` — stored the raw exception message.
- **Problem:** Exception messages can contain secrets (API keys in URLs, connection strings, PII). Storing them in last_error (which is visible in the DLQ and dashboard) leaks secrets.
- **Corrected:** Sanitize: store only the exception class name and a truncated (200 char) message. Day 6 adds a proper SecretRedactor for logs and traces.
- **Reason:** Secrets never in logs, traces, error messages, or DB columns.
- **Affected documents:** §03 (Engineering Contract — §4 Data handling), §12 (Observability).
- **Affected implementation:** `supervisor.py` — truncate and sanitize last_error.
- **Tests required:** `test_exception_message_sanitized`.

---

## Correction 19: Idempotency records are typed, not dicts

- **Original:** `dict[str, Any]` for idempotency records.
- **Problem:** Untyped dicts are error-prone. Fields can be missing or misnamed without detection.
- **Corrected:** Define `IdempotencyRecord` as a Pydantic model with explicit fields: key, run_id, workspace_id, step_id, tool_name, status, result, error, created_at, updated_at.
- **Reason:** Type safety. Pydantic validates on every read/write.
- **Affected documents:** §07 (Data Model — idempotency table).
- **Affected implementation:** `models.py` — add `IdempotencyRecord`. `state_store.py` — use it instead of dict.
- **Tests required:** `test_idempotency_record_typed`, `test_missing_idempotency_record_behavior`.

---

## Summary of affected Day 1 files

| File | Changes |
|---|---|
| `opsmesh/state_machine.py` | FAILED is truly terminal (set()); SEND_UNKNOWN transitions documented as reconciler-only. |
| `opsmesh/models.py` | Add `version`, `parent_run_id` fields; add `LeadDisposition`, `CRMOutboxStatus`, `CRMOutboxOp`, `IdempotencyRecord`, `Lead`, `ContactPolicy` models. |
| `opsmesh/state_store.py` | ABC (not Protocol terminology); `expected_version` for CAS; `IdempotencyRecord` instead of dict; defensive copies; workspace-scoped. |
| `opsmesh/supervisor.py` | While loop + dynamic `_next_step`; Critic retry limit (max 2); unknown verdict → FAILED; sanitized exceptions; Gate 1 transition on insufficient evidence. |
| `tests/conftest.py` | Spies/counters on mock specialists; fixtures for insufficient evidence, NEEDS_REVIEW, critic rejection, max attempts, malformed result, unexpected exception. |
| `tests/unit/test_state_machine.py` | FAILED is truly terminal; SEND_UNKNOWN tests updated. |
| `tests/unit/test_models.py` | NEW — Pydantic validation, enum representation, constraints, evidence refs. |
| `tests/unit/test_state_store.py` | NEW — workspace scoping, monotonic versioning, CAS, defensive copies, idempotency. |
| `tests/unit/test_supervisor.py` | Dynamic workflow, critic retry, sanitization, spies for non-repetition. |
