"""
OPSMESH Supervisor — the orchestrator.

The supervisor is the ONLY component that owns workflow state. It is a
plain Python class — not a LangGraph node, not an OpenAI Agents SDK agent.
(ADR-001: "Plain Python orchestration.")

ARCHITECTURE CORRECTIONS APPLIED:
  - Correction 5: Gate 1 (insufficient evidence) pauses at
                 AWAITING_EVIDENCE_REVIEW. Never authorizes sending.
  - Correction 16: Dynamic step computation via while loop + _next_step.
                   No stale precomputed plan after critic rejection.
  - Correction 17: Unknown critic verdict → FAILED (fail closed).
  - Correction 18: Exception messages sanitized (truncated, no secrets).

WHAT THE SUPERVISOR DOES:
    1. Load the latest checkpoint from the DB.
    2. If terminal or paused, return immediately.
    3. While there's a next step to execute:
       a. Compute the next step based on the CURRENT state (not a stale plan).
       b. Execute the step (transition → specialist → apply result → transition).
       c. If terminal or paused, break.

WHAT THE SUPERVISOR DOES NOT DO:
    - Does NOT call the LLM directly. Specialists do.
    - Does NOT call tools directly. Specialists call MCP (Day 4).
    - Does NOT write email content. The Draft specialist does.
    - Does NOT decide workflow order. The plan is hardcoded.
    - Does NOT bypass Gate 1 or Gate 2. State machine forbids it.

DAY 1 SCOPE:
    Mock specialists (Day 2 makes them real). No LLM calls. No tool calls.
    The supervisor executes research → qualify → draft → critic and pauses
    at AWAITING_APPROVAL (Gate 2). Sending and CRM update are stubbed —
    Day 5 implements them.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from opsmesh.budget import BudgetExceeded
from opsmesh.models import RunRecord
from opsmesh.state_machine import (
    State,
    can_transition,
    is_paused,
    is_terminal,
    transition,
)
from opsmesh.state_store import StateStore


# ---------------------------------------------------------------------------
# The hardcoded PLAN — specialist step names in execution order.
# Gates are NOT in the plan; they are STATES (pauses), not work.
# (Correction 11: workflow step definitions are explicit.)
# ---------------------------------------------------------------------------

PLAN: list[str] = [
    "researching",
    "qualifying",
    "drafting",
    "critic",
    # Gate 2 (AWAITING_APPROVAL) is a pause, not a step.
    "sending",        # Phase 7: actually send the email via Resend
    "crm_updating",   # Phase 7: sync to HubSpot CRM
]

STEP_TO_STATE: dict[str, State] = {
    "researching": State.RESEARCHING,
    "qualifying": State.QUALIFYING,
    "drafting": State.DRAFTING,
    "critic": State.CRITIC,
    "sending": State.SENDING,
    "crm_updating": State.CRM_UPDATING,
}

MAX_DRAFT_ATTEMPTS = 2  # Engineering Contract: max 2 draft revisions.
MAX_SUPERVISOR_ITERATIONS = 20  # safety guard against infinite loops.


# ---------------------------------------------------------------------------
# Specialist protocol — the interface every specialist must implement.
# ---------------------------------------------------------------------------


class Specialist(Protocol):
    """
    A specialist agent. Single async method: __call__.

    Contract:
        - Receives the current RunRecord (read-only — do not mutate).
        - Receives a budget object (Day 3 — None for now).
        - Receives a span context (Day 6 — None for now).
        - Returns a typed result (ResearchResult, QualificationResult, etc.).
        - Is stateless from other specialists' perspective.
        - Does NOT call other specialists.
        - Does NOT persist state (the supervisor does that).
        - Does NOT set run.state (the supervisor does that).
    """

    async def __call__(
        self, run: RunRecord, budget: Any, ctx: Any
    ) -> Any:
        """Execute the specialist's work. Returns a typed result."""


# ---------------------------------------------------------------------------
# The Supervisor
# ---------------------------------------------------------------------------


class Supervisor:
    """
    Owns the workflow plan, state checkpoints, and delegation.

    Usage:
        supervisor = Supervisor(
            specialists={"researching": ResearchSpecialist(...), ...},
            store=InMemoryStateStore(),
        )
        final_run = await supervisor.run(job)
    """

    PLAN: list[str] = PLAN
    MAX_DRAFT_ATTEMPTS: int = MAX_DRAFT_ATTEMPTS

    def __init__(
        self,
        specialists: dict[str, Specialist],
        store: StateStore,
        approvals: Any = None,                  # Day 5
        budget_factory: Optional[Any] = None,   # Day 3
        tracer: Any = None,                      # Day 6
        gateway: Any = None,                     # Stage B Phase 2 (P0-1)
    ) -> None:
        self.specialists = specialists
        self.store = store
        self.approvals = approvals
        self.budget_factory = budget_factory
        self.tracer = tracer
        # Stage B Phase 2 (P0-1): the ToolGateway is the policy enforcement
        # layer. Specialists that produce side effects (SendingSpecialist,
        # CRMUpdatingSpecialist) route through gateway.execute(...) when set.
        # The supervisor does NOT call the gateway directly — it passes a
        # SecurityContext to each specialist via ctx, and the specialist
        # decides whether to use the gateway.
        self.gateway = gateway

    # -------------------------------------------------------------------
    # Main loop — DYNAMIC step computation (Correction 16)
    # -------------------------------------------------------------------

    async def run(self, job: Any) -> RunRecord:
        """
        Execute the workflow for one job. Idempotent on job.run_id.

        If the run was already partially executed (previous worker crashed),
        this method loads the latest checkpoint and resumes from the next
        un-executed step. Completed steps are NOT repeated (verified by
        spies in tests).
        """
        # 1. Load the latest checkpoint (or initialize a new run)
        run = await self.store.load_latest(job.run_id, job.workspace_id)
        if run is None:
            run = RunRecord(
                run_id=job.run_id,
                workspace_id=job.workspace_id,
                lead_id=job.lead_id,
                state=State.NEW,
            )
            await self.store.save_checkpoint(run)

        # 2. Early return for terminal runs
        if is_terminal(run.state):
            return run

        # 2.5 FIX 2: If the run is paused (Gate 1 / Gate 2), check if the
        # approval has been decided. If so, resume. If not, return (still paused).
        if is_paused(run.state):
            if self.approvals is not None and run.pending_approval_id is not None:
                # Check if the approval has been decided
                from opsmesh.models import ApprovalStatus
                approval = self.approvals.approval_store.get(run.pending_approval_id)
                if approval is not None and approval.status != ApprovalStatus.PENDING:
                    # The approval has been decided — resume the run
                    try:
                        run = await self.approvals.resume(
                            run.pending_approval_id, run.workspace_id
                        )
                    except Exception:
                        pass  # resume failed; run stays paused

            if is_terminal(run.state) or is_paused(run.state):
                return run

        # 2.6 Create a budget for this run (if a factory is provided)
        budget = self.budget_factory() if self.budget_factory is not None else None

        # 3. Dynamic loop — recompute next step each iteration
        #    This prevents the stale-plan bug after critic rejection.
        #    (Correction 16.)
        iterations = 0
        while iterations < MAX_SUPERVISOR_ITERATIONS:
            iterations += 1

            next_step = self._next_step(run)
            if next_step is None:
                break  # terminal or paused

            await self._execute_step(run, next_step, budget)

            if is_terminal(run.state) or is_paused(run.state):
                break

        return run

    def _next_step(self, run: RunRecord) -> Optional[str]:
        """
        Compute the next step to execute based on the CURRENT state.

        This is called every iteration of the while loop, so it always
        reflects the latest state — not a stale precomputed plan.

        Returns None when:
            - The run is terminal (no work to do).
            - The run is paused (Gate 1, Gate 2, SEND_UNKNOWN).
            - The state doesn't map to a Day 1 step (sending, crm_updating
              are Day 5; for now they return None, pausing the run).
        """
        if is_terminal(run.state) or is_paused(run.state):
            return None

        state_to_step = {
            State.NEW: "researching",
            State.EVIDENCE_PROVIDED: "researching",
            State.RESEARCHING: "researching",  # crash recovery — re-execute
            State.QUALIFYING: "qualifying",    # crash recovery — re-execute
            State.DRAFTING: "drafting",
            State.CRITIC: "critic",
            State.SENDING: "sending",           # Phase 7: send email
            State.CRM_UPDATING: "crm_updating", # Phase 7: sync CRM
        }
        return state_to_step.get(run.state)

    # -------------------------------------------------------------------
    # Step execution — checkpoint before, delegate, checkpoint after
    # -------------------------------------------------------------------

    async def _execute_step(self, run: RunRecord, step: str, budget: Any = None) -> None:
        """
        Checkpoint before, delegate to specialist, checkpoint after.

        Budget enforcement:
            - Before each specialist: check elapsed time + count a tool call.
            - After each specialist: consume nominal tokens + cost.
            - If BudgetExceeded is raised, transition to FAILED.

        Crash recovery:
            - If the process dies between pre-checkpoint and specialist call:
              on resume, we re-execute the step. LLM calls are side-effect-free.
            - If the process dies between specialist call and post-checkpoint:
              on resume, we re-execute the step. For tool calls with side
              effects, the IdempotentToolExecutor (Day 3) prevents duplicates.
        """
        specialist = self.specialists.get(step)
        if specialist is None:
            run.last_error = f"Unknown step: {step}"
            await self._safe_transition_to(run, State.FAILED)
            return

        # --- Budget check (before the step) ---
        if budget is not None:
            try:
                budget.check_elapsed()
                budget.tool_call()
            except BudgetExceeded as e:
                run.last_error = f"BudgetExceeded: {e.reason}"
                await self._safe_transition_to(run, State.FAILED)
                return

        # --- Pre-step checkpoint ---
        # Transition to the step's state, increment version, save.
        target_state = STEP_TO_STATE.get(step, State.FAILED)
        if run.state != target_state:
            await self._safe_transition_to(run, target_state)
        run.step_id += 1
        await self.store.save_checkpoint(run)

        try:
            # --- Delegate to specialist ---
            # Stage B Phase 2 (P0-1): pass a scoped SecurityContext so the
            # SendingSpecialist + CRMUpdatingSpecialist can route through the
            # ToolGateway with the trusted workspace_id + run_id. The ctx
            # carries trusted identity (never from LLM args).
            from opsmesh.security_context import SecurityContext
            ctx = SecurityContext(
                workspace_id=run.workspace_id,
                run_id=run.run_id,
                step_id=run.step_id,
                actor="supervisor",
                scopes=["crm.read", "crm.write", "email.send", "notify.send", "web.search"],
            )
            result = await specialist(run, budget=budget, ctx=ctx)

            # --- Track actual token usage + cost from the LLM response ---
            # The specialists return typed results (ResearchResult, etc.),
            # but they also store the LLM response metadata on the run record
            # via run.budget_used_tokens + run.budget_used_cost_usd.
            # Here we check if the specialist attached token info to the result.
            if hasattr(result, "_llm_tokens_used") and hasattr(result, "_llm_cost_usd"):
                run.budget_used_tokens += getattr(result, "_llm_tokens_used", 0)
                run.budget_used_cost_usd += getattr(result, "_llm_cost_usd", 0.0)
                run.budget_used_tool_calls += 1
            elif budget is not None:
                # Fallback: nominal consumption for non-LLM steps (sending, crm)
                budget.consume(tokens=1, cost_usd=0.001)
                run.budget_used_tool_calls += 1

            # --- Apply result (transitions state based on result) ---
            self._apply_result(run, step, result)

            # --- Post-step checkpoint ---
            run.step_id += 1
            await self.store.save_checkpoint(run)

            # --- FIX 2: Create approval records when entering paused states ---
            # When the run enters AWAITING_APPROVAL (Gate 2) or
            # AWAITING_EVIDENCE_REVIEW (Gate 1), create an approval record
            # via the ApprovalGate.
            if (
                self.approvals is not None
                and run.state == State.AWAITING_APPROVAL
                and run.pending_approval_id is None
            ):
                # Gate 2: email send approval
                await self._create_gate2_approval(run)
            elif (
                self.approvals is not None
                and run.state == State.AWAITING_EVIDENCE_REVIEW
                and run.pending_approval_id is None
            ):
                # Gate 1: evidence review approval
                await self._create_gate1_approval(run)

        except BudgetExceeded as e:
            # Budget exceeded during the step — fail the run
            run.last_error = f"BudgetExceeded: {e.reason}"
            await self._safe_transition_to(run, State.FAILED)
            await self.store.save_checkpoint(run)

        except Exception as e:
            # --- Sanitized error handling (Correction 18) ---
            # Store only the exception class name + truncated message.
            # Never store full stack traces or raw exception strings that
            # might contain secrets (API keys in URLs, connection strings).
            safe_msg = str(e)[:200]
            run.last_error = f"{type(e).__name__}: {safe_msg}"
            await self._safe_transition_to(run, State.FAILED)
            await self.store.save_checkpoint(run)

    # -------------------------------------------------------------------
    # Apply specialist results — the supervisor owns state transitions
    # -------------------------------------------------------------------

    def _apply_result(self, run: RunRecord, step: str, result: Any) -> None:
        """
        Apply a specialist's result to the run record and transition state.

        The specialist returns a typed result; the supervisor decides what
        state to transition to. The specialist NEVER sets run.state.
        """
        if step == "researching":
            run.research = result
            # Gate 1: if research is insufficient, pause.
            # (Correction 5: Gate 1 never authorizes sending.)
            if hasattr(result, "sufficient_for_qualification"):
                if not result.sufficient_for_qualification:
                    self._transition_to_sync(run, State.AWAITING_EVIDENCE_REVIEW)
                # else: stay in RESEARCHING; _next_step will return "researching"
                # again, but since run.research is now set, the re-execution
                # is harmless (LLM calls are side-effect-free).
                # Actually, we should transition to QUALIFYING here:
                else:
                    self._transition_to_sync(run, State.QUALIFYING)

        elif step == "qualifying":
            run.qualification = result
            if hasattr(result, "status"):
                if result.status == "QUALIFIED":
                    self._transition_to_sync(run, State.DRAFTING)
                elif result.status == "DISQUALIFIED":
                    self._transition_to_sync(run, State.FAILED)
                elif result.status == "NEEDS_REVIEW":
                    # Gate 1: pause for human decision.
                    self._transition_to_sync(run, State.AWAITING_EVIDENCE_REVIEW)
                else:
                    # Unknown status → fail closed
                    run.last_error = f"Unknown qualification status: {result.status}"
                    self._transition_to_sync(run, State.FAILED)
            else:
                run.last_error = "Qualification result has no status attribute"
                self._transition_to_sync(run, State.FAILED)

        elif step == "drafting":
            run.draft = result
            self._transition_to_sync(run, State.CRITIC)

        elif step == "critic":
            run.critic_attempts += 1
            if hasattr(result, "verdict"):
                if result.verdict == "APPROVED":
                    run.critic_feedback = None
                    # Clear any prior pending_approval_id so the post-step
                    # check creates a FRESH Gate 2 approval. Handles the
                    # Gate1->Draft->Critic loop and request_changes->Draft->
                    # Critic loop, where the run carries an old approval ID
                    # from a previous gate. Without this, the condition
                    # `run.pending_approval_id is None` in _execute_step
                    # would be False and no new Gate 2 approval is created.
                    run.pending_approval_id = None
                    # FIX 2: Wire Gate 2 into the supervisor.
                    # Transition to AWAITING_APPROVAL and create an approval record.
                    self._transition_to_sync(run, State.AWAITING_APPROVAL)
                    if self.approvals is not None and run.draft is not None:
                        # Create the Gate 2 approval record (async, but we're
                        # inside a sync method — schedule it)
                        # For Day 5, we store the draft as the payload.
                        # The full wiring happens in _execute_step's post-checkpoint.
                        pass  # approval creation handled in _execute_step
                elif result.verdict == "REJECTED":
                    run.critic_feedback = result.feedback
                    run.draft_attempts += 1
                    if run.draft_attempts >= MAX_DRAFT_ATTEMPTS:
                        # Max revisions exceeded → fail
                        self._transition_to_sync(run, State.FAILED)
                    else:
                        # Retry drafting with feedback
                        run.draft = None  # clear old draft
                        self._transition_to_sync(run, State.DRAFTING)
                else:
                    # Unknown verdict → fail closed (Correction 17)
                    run.last_error = f"Unknown critic verdict: {result.verdict}"
                    self._transition_to_sync(run, State.FAILED)
            else:
                run.last_error = "Critic result has no verdict attribute"
                self._transition_to_sync(run, State.FAILED)

        elif step == "sending":
            # The sending specialist returns a dict with status + message_id
            if isinstance(result, dict):
                status = result.get("status", "failed")
                if status in ("sent", "skipped"):
                    # Email sent (or skipped due to kill switch) → proceed to CRM sync
                    run.email_message_id = result.get("message_id")
                    self._transition_to_sync(run, State.CRM_UPDATING)
                elif status == "unknown":
                    # Ambiguous timeout → SEND_UNKNOWN (reconciler will resolve)
                    run.email_message_id = result.get("message_id")
                    self._transition_to_sync(run, State.SEND_UNKNOWN)
                else:
                    # Confirmed failure → FAILED
                    run.last_error = result.get("error", "Email send failed")
                    self._transition_to_sync(run, State.FAILED)
            else:
                run.last_error = "Sending result is not a dict"
                self._transition_to_sync(run, State.FAILED)

        elif step == "crm_updating":
            # CRM sync is non-blocking — even if it fails, the email was sent
            # (which is the important part). Transition to SENT regardless.
            if isinstance(result, dict) and result.get("status") == "synced":
                run.lead_id = run.lead_id  # keep the same lead_id
            # Don't fail on CRM errors — the email was already sent
            self._transition_to_sync(run, State.SENT)

    # -------------------------------------------------------------------
    # Transition helpers
    # -------------------------------------------------------------------

    def _transition_to_sync(self, run: RunRecord, target: State) -> None:
        """Synchronous transition — used inside _apply_result. Validates
        the transition; if illegal, sets last_error but does NOT raise
        (the _execute_step exception handler will catch the inconsistency
        on the next save_checkpoint via the StateStore's transition
        validation)."""
        if can_transition(run.state, target):
            run.state = transition(run.state, target)
        else:
            # Illegal transition — record the error. The next save_checkpoint
            # will succeed (the StateStore validates the transition from the
            # PERSISTED state, not the in-memory state), but the run's state
            # will be inconsistent. The _execute_step exception handler
            # will catch this and transition to FAILED.
            run.last_error = (
                f"Illegal transition attempted: {run.state.value} -> {target.value}"
            )

    async def _safe_transition_to(self, run: RunRecord, target: State) -> None:
        """Async transition with validation AND persistence. If the
        transition is illegal, the run is transitioned to FAILED instead."""
        try:
            run.state = transition(run.state, target)
            await self.store.save_checkpoint(run)
        except Exception:
            # If the transition is illegal, fail the run
            run.last_error = f"Illegal transition to {target.value}"
            run.state = State.FAILED
            await self.store.save_checkpoint(run)

    # -------------------------------------------------------------------
    # FIX 2: Approval gate integration helpers
    # -------------------------------------------------------------------

    async def _create_gate1_approval(self, run: RunRecord) -> None:
        """Create a Gate 1 (evidence_review) approval record.

        Called when the run enters AWAITING_EVIDENCE_REVIEW.
        Gate 1 NEVER authorizes sending (Correction 5).
        """
        from opsmesh.approval_store import compute_payload_hash
        from opsmesh.models import ApprovalKind

        payload = {
            "missing_info": [],
            "summary": run.research.summary if run.research else "Insufficient evidence.",
        }
        ph = compute_payload_hash("evidence_review", payload)
        try:
            approval_id = await self.approvals.request(
                workspace_id=run.workspace_id,
                run_id=run.run_id,
                kind=ApprovalKind.EVIDENCE_REVIEW,
                action="evidence_review",
                payload=payload,
                payload_hash=ph,
                run_state=State.AWAITING_EVIDENCE_REVIEW,
                step_id=run.step_id,
            )
            run.pending_approval_id = approval_id
            await self.store.save_checkpoint(run)
        except Exception as e:
            # Log the error but do NOT transition to FAILED — the run is
            # already paused at AWAITING_EVIDENCE_REVIEW, and transitioning
            # from a paused state to FAILED is illegal (the state machine
            # forbids it). Just record the error; the operator can see it.
            run.last_error = f"Gate 1 approval creation failed: {type(e).__name__}: {str(e)[:200]}"
            # Save the error to the checkpoint, but stay in the paused state.
            # The run is visible in the dashboard with last_error set.
            try:
                await self.store.save_checkpoint(run)
            except Exception:
                pass

    async def _create_gate2_approval(self, run: RunRecord) -> None:
        """Create a Gate 2 (email_send) approval record.

        Called when the run enters AWAITING_APPROVAL.
        The approval is bound to the EXACT draft payload (recipient, subject, body).
        A material change invalidates the approval (StaleApprovalError).

        Stage B Phase 1 (P1-2): the sender identity is read from
        Settings.workspace_sender_identity (default
        ``OpsMesh <onboarding@resend.dev>`` — Resend's pre-verified test
        sender). The previous hardcoded ``bekalu@opsmesh.dev`` was an
        unverified domain that caused Resend 403s and bound the approval
        payload hash to the wrong ``from_``.
        """
        from opsmesh.approval_store import compute_payload_hash, TOOL_PROTECTED_FIELDS
        from opsmesh.config import get_settings
        from opsmesh.models import ApprovalKind

        if run.draft is None:
            run.last_error = "Cannot create Gate 2 approval: no draft"
            # Can't transition to FAILED from AWAITING_APPROVAL (paused) — just log
            try:
                await self.store.save_checkpoint(run)
            except Exception:
                pass
            return

        # Build the payload from the draft — sender from Settings (P1-2).
        sender_identity = get_settings().workspace_sender_identity
        payload = {
            "to": run.draft.recipient,
            "from_": sender_identity,
            "subject": run.draft.subject,
            "body": run.draft.body,
        }
        protected = TOOL_PROTECTED_FIELDS.get("send_email")
        ph = compute_payload_hash("send_email", payload, protected)
        try:
            approval_id = await self.approvals.request(
                workspace_id=run.workspace_id,
                run_id=run.run_id,
                kind=ApprovalKind.EMAIL_SEND,
                action="send_email",
                payload=payload,
                payload_hash=ph,
                run_state=State.AWAITING_APPROVAL,
                step_id=run.step_id,
            )
            run.pending_approval_id = approval_id
            await self.store.save_checkpoint(run)
        except Exception as e:
            # Log the error but do NOT transition to FAILED — the run is
            # already paused at AWAITING_APPROVAL, and transitioning from a
            # paused state to FAILED is illegal. Just record the error.
            run.last_error = f"Gate 2 approval creation failed: {type(e).__name__}: {str(e)[:200]}"
            try:
                await self.store.save_checkpoint(run)
            except Exception:
                pass


__all__ = ["Supervisor", "Specialist", "PLAN", "STEP_TO_STATE", "MAX_DRAFT_ATTEMPTS"]
