import pytest

from app.jobs.state import ALLOWED_TRANSITIONS, TERMINAL, JobStateError, validate_transition


def test_all_expected_transitions_valid():
    cases = [
        ("pending", "running"),  # claim
        ("pending", "cancelled"),
        ("retrying", "running"),  # backoff elapsed
        ("retrying", "cancelled"),
        ("running", "completed"),
        ("running", "failed"),
        ("running", "cancelled"),
        ("running", "retrying"),  # transient failure
        ("running", "running"),  # lease re-claim after crash
        ("failed", "retrying"),  # manual retry
    ]
    for current, target in cases:
        validate_transition(current, target)


def test_illegal_transitions_raise():
    illegal = [
        ("completed", "retrying"),
        ("cancelled", "running"),
        ("completed", "failed"),
        ("pending", "completed"),
        ("retrying", "completed"),
        ("pending", "failed"),
    ]
    for current, target in illegal:
        with pytest.raises(JobStateError):
            validate_transition(current, target)


def test_unknown_state_raises():
    with pytest.raises(JobStateError):
        validate_transition("nope", "pending")


def test_terminal_states_complete():
    # "failed" can transition to retrying (manual retry); completed/cancelled are final.
    assert "failed" in TERMINAL
    assert not ALLOWED_TRANSITIONS["completed"]
    assert not ALLOWED_TRANSITIONS["cancelled"]
    assert ALLOWED_TRANSITIONS["failed"] == {"retrying", "cancelled"}
