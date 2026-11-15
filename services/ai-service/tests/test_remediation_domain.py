"""The status table of remediation: every transition the diagram draws, and none it does not."""

import pytest

from app.schemas.remediation import (
    ACTIVE_STATUSES,
    STATUSES,
    TERMINAL_STATUSES,
    TRANSITIONS,
    WAITING_FOR_A_HUMAN,
    can_transition,
)


def _reachable(start: str) -> set[str]:
    seen, queue = {start}, [start]
    while queue:
        for nxt in TRANSITIONS[queue.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def test_every_status_has_a_row_and_every_target_is_a_status() -> None:
    assert set(TRANSITIONS) == set(STATUSES)
    assert all(targets <= set(STATUSES) for targets in TRANSITIONS.values())


def test_the_vocabulary_has_no_duplicates() -> None:
    assert len(STATUSES) == len(set(STATUSES))


def test_everything_is_reachable_from_the_offer() -> None:
    assert _reachable("OFFERED") == set(STATUSES)


def test_every_status_can_reach_an_end() -> None:
    for status in STATUSES:
        assert _reachable(status) & TERMINAL_STATUSES, status  # no state traps a run for ever


def test_nothing_leaves_a_terminal_status() -> None:
    assert TERMINAL_STATUSES == {"VERIFIED", "ROLLED_BACK", "DECLINED", "ABANDONED", "FAILED", "CANCELLED"}
    assert all(not TRANSITIONS[status] for status in TERMINAL_STATUSES)


def test_an_offer_does_not_hold_the_environment_but_a_started_run_does() -> None:
    assert "OFFERED" not in ACTIVE_STATUSES
    assert ACTIVE_STATUSES == set(STATUSES) - TERMINAL_STATUSES - {"OFFERED"}
    assert WAITING_FOR_A_HUMAN <= ACTIVE_STATUSES


@pytest.mark.parametrize(
    ("current", "new"),
    [
        ("OFFERED", "PREFLIGHT"),
        ("PREFLIGHT", "AWAITING_DEPLOY_APPROVAL"),
        ("AWAITING_DEPLOY_APPROVAL", "DEPLOYING"),
        ("DEPLOYING", "VERIFYING"),
        ("VERIFYING", "VERIFIED"),
        ("VERIFYING", "AWAITING_DECISION"),
        ("AWAITING_DECISION", "VERIFYING"),  # extend the soak
        ("VERIFYING", "ROLLING_BACK"),  # automatic rollback, for a tenant that opted in
        ("AWAITING_ROLLBACK_APPROVAL", "ROLLING_BACK"),
        ("ROLLING_BACK", "ROLLED_BACK"),
    ],
)
def test_the_paths_the_diagram_draws_are_allowed(current: str, new: str) -> None:
    assert can_transition(current, new)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        ("OFFERED", "DEPLOYING"),  # a deploy always goes through preflight and a human
        ("PREFLIGHT", "DEPLOYING"),
        ("AWAITING_DEPLOY_APPROVAL", "VERIFYING"),
        ("DEPLOYING", "ROLLING_BACK"),  # nothing to roll back before the deploy succeeded
        ("VERIFIED", "ROLLING_BACK"),
        ("ROLLED_BACK", "ROLLING_BACK"),  # one rollback only
        ("FAILED", "PREFLIGHT"),
        ("DECLINED", "AWAITING_DEPLOY_APPROVAL"),
        ("ROLLING_BACK", "VERIFYING"),
        ("OFFERED", "OFFERED"),
    ],
)
def test_the_shortcuts_the_design_forbids_are_refused(current: str, new: str) -> None:
    assert not can_transition(current, new)


def test_unknown_statuses_are_never_allowed() -> None:
    assert not can_transition("OFFERED", "PAUSED")
    assert not can_transition("PAUSED", "OFFERED")
