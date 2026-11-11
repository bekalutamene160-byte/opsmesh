"""
Tests for opsmesh.state_machine.

Covers:
    - State enum completeness (16 states, pinned)
    - Every allowed transition succeeds
    - Important forbidden transitions raise IllegalTransition
    - Raw string and non-State type rejection
    - Terminal-state behavior (ALL terminal, including FAILED — Correction 4)
    - Paused-state behavior
    - SEND_UNKNOWN behavior (Correction 3)
    - Invalid transitions do not mutate state
"""

from __future__ import annotations

import pytest

from opsmesh.state_machine import (
    ALLOWED_TRANSITIONS,
    PAUSED_STATES,
    TERMINAL_STATES,
    IllegalTransition,
    State,
    can_transition,
    is_paused,
    is_terminal,
    transition,
)


# ---------------------------------------------------------------------------
# 1. State enum completeness (Correction 1: 16 states)
# ---------------------------------------------------------------------------


class TestStateEnum:
    EXPECTED_STATES = {
        "new", "researching", "awaiting_evidence_review", "evidence_provided",
        "qualifying", "drafting", "critic",
        "awaiting_approval",
        "sending", "send_unknown", "crm_updating",
        "sent", "rejected", "failed", "stopped_by_user", "approval_expired",
    }

    def test_state_count_is_16(self):
        """Pin the count at 16. Architecture prose said 13 (typo)."""
        assert len(State) == 16

    def test_all_expected_states_present(self):
        actual = {s.value for s in State}
        assert actual == self.EXPECTED_STATES

    def test_state_is_str_enum(self):
        """str inheritance makes State JSON-serializable."""
        assert State.NEW == "new"
        assert State.SENDING == "sending"

    def test_state_values_are_unique(self):
        values = [s.value for s in State]
        assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# 2. Every allowed transition succeeds
# ---------------------------------------------------------------------------


class TestAllowedTransitions:
    def test_all_allowed_transitions_succeed(self):
        for from_state, to_states in ALLOWED_TRANSITIONS.items():
            for to_state in to_states:
                assert transition(from_state, to_state) == to_state

    def test_can_transition_returns_true_for_allowed(self):
        for from_state, to_states in ALLOWED_TRANSITIONS.items():
            for to_state in to_states:
                assert can_transition(from_state, to_state) is True

    def test_happy_path_sequence(self):
        path = [
            (State.NEW, State.RESEARCHING),
            (State.RESEARCHING, State.QUALIFYING),
            (State.QUALIFYING, State.DRAFTING),
            (State.DRAFTING, State.CRITIC),
            (State.CRITIC, State.AWAITING_APPROVAL),
            (State.AWAITING_APPROVAL, State.SENDING),
            (State.SENDING, State.CRM_UPDATING),
            (State.CRM_UPDATING, State.SENT),
        ]
        for current, target in path:
            assert transition(current, target) == target

    def test_gate1_path_sequence(self):
        assert transition(State.RESEARCHING, State.AWAITING_EVIDENCE_REVIEW) == State.AWAITING_EVIDENCE_REVIEW
        assert transition(State.AWAITING_EVIDENCE_REVIEW, State.EVIDENCE_PROVIDED) == State.EVIDENCE_PROVIDED
        assert transition(State.EVIDENCE_PROVIDED, State.RESEARCHING) == State.RESEARCHING

    def test_send_unknown_reconciliation_paths(self):
        """SEND_UNKNOWN → SENT and → FAILED are legal (reconciler-only)."""
        assert transition(State.SENDING, State.SEND_UNKNOWN) == State.SEND_UNKNOWN
        assert transition(State.SEND_UNKNOWN, State.SENT) == State.SENT
        assert transition(State.SEND_UNKNOWN, State.FAILED) == State.FAILED


# ---------------------------------------------------------------------------
# 3. Forbidden transitions (security-critical)
# ---------------------------------------------------------------------------


class TestForbiddenTransitions:
    @pytest.mark.parametrize("from_state,target", [
        # INV-1: cannot enter SENDING except from AWAITING_APPROVAL
        (State.CRITIC, State.SENDING),
        (State.DRAFTING, State.SENDING),
        (State.RESEARCHING, State.SENDING),
        (State.NEW, State.SENDING),

        # INV-2: cannot skip Gate 2
        (State.AWAITING_APPROVAL, State.SENT),
        (State.AWAITING_APPROVAL, State.CRM_UPDATING),

        # INV-3: cannot skip research + qualification
        (State.NEW, State.DRAFTING),
        (State.NEW, State.AWAITING_APPROVAL),
        (State.RESEARCHING, State.DRAFTING),

        # INV-6: no transitions out of terminal states
        # (Correction 4: FAILED is now truly terminal — no retry transition)
        (State.SENT, State.RESEARCHING),
        (State.SENT, State.FAILED),
        (State.REJECTED, State.SENDING),
        (State.FAILED, State.RESEARCHING),  # KEY: FAILED no longer allows retry
        (State.STOPPED_BY_USER, State.RESEARCHING),
        (State.APPROVAL_EXPIRED, State.AWAITING_APPROVAL),

        # Cannot update CRM if email send is ambiguous
        (State.SEND_UNKNOWN, State.CRM_UPDATING),

        # Cannot recover to SENT without re-running (new run)
        (State.FAILED, State.SENT),
    ])
    def test_forbidden_transition_raises(self, from_state: State, target: State):
        with pytest.raises(IllegalTransition) as exc_info:
            transition(from_state, target)
        assert from_state.value in str(exc_info.value)

    def test_can_transition_returns_false_for_forbidden(self):
        assert can_transition(State.CRITIC, State.SENDING) is False
        assert can_transition(State.SENT, State.RESEARCHING) is False
        assert can_transition(State.FAILED, State.RESEARCHING) is False

    def test_valid_state_string_is_coerced(self):
        """Valid state strings are coerced to State enums (not rejected).
        This makes the system robust when loading from JSON or DB."""
        result = transition(State.NEW, "researching")  # type: ignore[arg-type]
        assert result == State.RESEARCHING
        assert isinstance(result, State)

    def test_can_transition_accepts_valid_string(self):
        assert can_transition(State.NEW, "researching") is True  # type: ignore[arg-type]

    def test_invalid_state_string_raises_type_error(self):
        """Invalid state strings (not a real State value) raise TypeError."""
        with pytest.raises(TypeError):
            transition(State.NEW, "not_a_state")  # type: ignore[arg-type]

    def test_can_transition_returns_false_for_invalid_string(self):
        assert can_transition(State.NEW, "not_a_state") is False  # type: ignore[arg-type]

    def test_non_state_object_raises_type_error(self):
        """Non-State objects (int, None) are rejected."""
        with pytest.raises(TypeError):
            transition(State.NEW, 42)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            transition(State.NEW, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. Terminal states (Correction 4: ALL terminal, including FAILED)
# ---------------------------------------------------------------------------


class TestTerminalStates:
    def test_all_terminal_states_have_empty_transitions(self):
        """ALL terminal states have set() — including FAILED.
        (Correction 4: FAILED is truly terminal. Retries create new runs.)"""
        for state in TERMINAL_STATES:
            assert ALLOWED_TRANSITIONS[state] == set(), (
                f"Terminal state {state} has outgoing transitions: "
                f"{ALLOWED_TRANSITIONS[state]}"
            )

    def test_sent_is_terminal(self):
        assert is_terminal(State.SENT)

    def test_rejected_is_terminal(self):
        assert is_terminal(State.REJECTED)

    def test_failed_is_truly_terminal(self):
        """Correction 4: FAILED has NO outgoing transitions. Retries
        create a new run with parent_run_id."""
        assert is_terminal(State.FAILED)
        assert ALLOWED_TRANSITIONS[State.FAILED] == set()

    def test_stopped_by_user_is_terminal(self):
        assert is_terminal(State.STOPPED_BY_USER)

    def test_approval_expired_is_terminal(self):
        assert is_terminal(State.APPROVAL_EXPIRED)


# ---------------------------------------------------------------------------
# 5. Paused states
# ---------------------------------------------------------------------------


class TestPausedStates:
    def test_gate1_pause_state_is_paused(self):
        assert is_paused(State.AWAITING_EVIDENCE_REVIEW)

    def test_gate2_pause_state_is_paused(self):
        assert is_paused(State.AWAITING_APPROVAL)

    def test_send_unknown_is_paused(self):
        assert is_paused(State.SEND_UNKNOWN)

    def test_non_paused_states(self):
        assert not is_paused(State.NEW)
        assert not is_paused(State.RESEARCHING)
        assert not is_paused(State.SENT)

    def test_no_overlap_between_terminal_and_paused(self):
        assert TERMINAL_STATES.isdisjoint(PAUSED_STATES)


# ---------------------------------------------------------------------------
# 6. Transition purity and non-mutation
# ---------------------------------------------------------------------------


class TestTransitionPurity:
    def test_returns_target_on_success(self):
        result = transition(State.NEW, State.RESEARCHING)
        assert result == State.RESEARCHING
        assert result is State.RESEARCHING

    def test_idempotent_calls(self):
        r1 = transition(State.NEW, State.RESEARCHING)
        r2 = transition(State.NEW, State.RESEARCHING)
        assert r1 == r2

    def test_illegal_transition_does_not_mutate_input(self):
        """If transition() raises, the State enum values are unchanged.
        (States are immutable enums, but this test documents the invariant.)"""
        original_new = State.NEW
        with pytest.raises(IllegalTransition):
            transition(State.NEW, State.SENT)
        assert State.NEW is original_new
        assert State.NEW == "new"

    def test_allowed_transitions_dict_is_not_mutated(self):
        """Calling transition() must not modify ALLOWED_TRANSITIONS."""
        original = {k: set(v) for k, v in ALLOWED_TRANSITIONS.items()}
        with pytest.raises(IllegalTransition):
            transition(State.NEW, State.SENT)
        assert ALLOWED_TRANSITIONS == original
