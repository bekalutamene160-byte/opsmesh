"""
OPSMESH State Machine — the contract.

Defines the 16 states a run can be in and the only legal transitions between
them. If the supervisor tries an illegal move, IllegalTransition is raised
and nothing is persisted.

ARCHITECTURE CORRECTIONS APPLIED (see docs/architecture/CORRECTIONS.md):
  - Correction 1: 16 states (not 13). Pinned in tests.
  - Correction 3: SEND_UNKNOWN transitions are reconciler-only.
  - Correction 4: FAILED is truly terminal — set(), not {RESEARCHING}.
                  Retries create a NEW run with parent_run_id.
"""

from __future__ import annotations

from enum import Enum
from typing import Set


class State(str, Enum):
    """
    The 16 states an OPSMESH run can be in.

    Inherits from `str` so JSON serialization produces "researching" (not
    "State.RESEARCHING"), making it human-readable in the DB and in psql.
    """

    # --- Initial ---
    NEW = "new"

    # --- Research + Gate 1 ---
    RESEARCHING = "researching"
    AWAITING_EVIDENCE_REVIEW = "awaiting_evidence_review"
    EVIDENCE_PROVIDED = "evidence_provided"

    # --- Qualify + Draft + Critic ---
    QUALIFYING = "qualifying"
    DRAFTING = "drafting"
    CRITIC = "critic"

    # --- Gate 2 (email approval) ---
    AWAITING_APPROVAL = "awaiting_approval"

    # --- Email send (3 separate states) ---
    SENDING = "sending"
    SEND_UNKNOWN = "send_unknown"

    # --- CRM update (post-send, outbox commit phase) ---
    CRM_UPDATING = "crm_updating"

    # --- Terminal states (no transitions out) ---
    SENT = "sent"
    REJECTED = "rejected"
    FAILED = "failed"
    STOPPED_BY_USER = "stopped_by_user"
    APPROVAL_EXPIRED = "approval_expired"


# Terminal states — the run is over. The supervisor's loop exits.
# ALL terminal states have empty transition sets, including FAILED.
# (Correction 4: FAILED is truly terminal. Retries create a new run.)
TERMINAL_STATES: Set[State] = {
    State.SENT,
    State.REJECTED,
    State.FAILED,
    State.STOPPED_BY_USER,
    State.APPROVAL_EXPIRED,
}

# Paused states — the run waits on external input (human decision or
# reconciler). No HTTP request is held open. The state IS the resume point.
PAUSED_STATES: Set[State] = {
    State.AWAITING_EVIDENCE_REVIEW,
    State.AWAITING_APPROVAL,
    State.SEND_UNKNOWN,
}


# ---------------------------------------------------------------------------
# ALLOWED_TRANSITIONS — the heart of the contract.
#
# Any transition not in this dict raises IllegalTransition. Nothing is
# persisted when an illegal transition is attempted.
# ---------------------------------------------------------------------------
ALLOWED_TRANSITIONS: dict[State, Set[State]] = {
    # --- Happy path ---
    State.NEW: {State.RESEARCHING, State.FAILED},
    State.RESEARCHING: {
        State.QUALIFYING,
        State.AWAITING_EVIDENCE_REVIEW,
        State.FAILED,
    },
    State.QUALIFYING: {
        State.DRAFTING,
        State.AWAITING_EVIDENCE_REVIEW,
        State.FAILED,
    },
    State.DRAFTING: {State.CRITIC, State.FAILED},
    State.CRITIC: {
        State.AWAITING_APPROVAL,
        State.DRAFTING,
        State.FAILED,
    },

    # --- Gate 1: insufficient-evidence approval ---
    State.AWAITING_EVIDENCE_REVIEW: {
        State.EVIDENCE_PROVIDED,
        State.DRAFTING,
        State.STOPPED_BY_USER,
        State.APPROVAL_EXPIRED,
    },
    State.EVIDENCE_PROVIDED: {State.RESEARCHING},

    # --- Gate 2: email-send approval ---
    State.AWAITING_APPROVAL: {
        State.SENDING,
        State.REJECTED,
        State.DRAFTING,
        State.APPROVAL_EXPIRED,
    },

    # --- Email send (3-state split) ---
    # SENDING → SEND_UNKNOWN is legal (ambiguous timeout).
    # SENDING → FAILED is legal (confirmed failure).
    # SENDING → CRM_UPDATING is legal (confirmed success).
    State.SENDING: {
        State.CRM_UPDATING,
        State.SEND_UNKNOWN,
        State.FAILED,
    },
    # SEND_UNKNOWN → SENT and → FAILED are legal ONLY when the reconciler
    # has CONCLUSIVE evidence from the provider. Never auto-transition
    # due to threshold exhaustion. (Correction 3.)
    State.SEND_UNKNOWN: {
        State.SENT,
        State.FAILED,
    },

    # --- CRM update (post-send) ---
    # CRM_UPDATING → SENT is unconditional: the outbox operation has been
    # durably committed; the run can close. The external CRM may still be
    # syncing. (Correction 2.)
    State.CRM_UPDATING: {State.SENT},

    # --- Terminal states: NO transitions out (Correction 4) ---
    # FAILED is truly terminal. Operator retry creates a NEW run with
    # parent_run_id linking to the original failed run.
    State.FAILED: set(),
    State.SENT: set(),
    State.REJECTED: set(),
    State.STOPPED_BY_USER: set(),
    State.APPROVAL_EXPIRED: set(),
}


class IllegalTransition(Exception):
    """
    Raised when a state transition is not in ALLOWED_TRANSITIONS.

    This is either a programming bug (supervisor tried an illegal move)
    or an attack signal (someone tried to forge state to bypass approval).
    Either way: fail loud, fail closed, persist nothing.
    """

    def __init__(self, current: State, target: object) -> None:
        self.current = current
        # target might be a raw string if the caller passed one by mistake
        target_str = target.value if isinstance(target, State) else str(target)
        allowed = ALLOWED_TRANSITIONS.get(current, set())
        allowed_str = sorted(s.value for s in allowed) or "<terminal>"
        super().__init__(
            f"Illegal transition: {current.value} -> {target_str}. "
            f"Allowed from {current.value}: {allowed_str}."
        )


def _coerce_state(value: object) -> State:
    """Convert a string to a State enum. Raises TypeError if not a State
    or a valid state string."""
    if isinstance(value, State):
        return value
    if isinstance(value, str):
        try:
            return State(value)
        except ValueError:
            raise TypeError(
                f"Invalid state string: '{value}'. Not a valid State."
            )
    raise TypeError(
        f"transition() requires State or str, got {type(value).__name__}."
    )


def transition(current: State, target: State) -> State:
    """
    Validate that current → target is legal. Return target on success.

    Pure function: no side effects. Called by the supervisor before
    persisting, and by the StateStore before writing (defense in depth).

    Accepts both State instances and valid state strings (coerces strings
    to State for convenience). Invalid strings raise TypeError.

    Raises:
        IllegalTransition: if the transition is not in ALLOWED_TRANSITIONS.
        TypeError: if target is not a State or a valid state string.
    """
    current_state = _coerce_state(current)
    target_state = _coerce_state(target)
    if target_state not in ALLOWED_TRANSITIONS.get(current_state, set()):
        raise IllegalTransition(current_state, target_state)
    return target_state


def can_transition(current: State, target: State) -> bool:
    """Non-raising variant — for conditional logic."""
    try:
        current_state = _coerce_state(current)
        target_state = _coerce_state(target)
    except TypeError:
        return False
    return target_state in ALLOWED_TRANSITIONS.get(current_state, set())


def is_terminal(state: State) -> bool:
    return state in TERMINAL_STATES


def is_paused(state: State) -> bool:
    return state in PAUSED_STATES


__all__ = [
    "State",
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATES",
    "PAUSED_STATES",
    "IllegalTransition",
    "transition",
    "can_transition",
    "is_terminal",
    "is_paused",
]
