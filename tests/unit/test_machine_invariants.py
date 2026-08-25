"""Property tests over the lifecycle graph.

These are the tests that make the safety story checkable rather than asserted.
They search the transition graph exhaustively instead of sampling a few happy
paths, so a future edit that quietly adds an edge from VIEW to DISPATCH fails
here rather than in production.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from forge.core.enums import TERMINAL_PHASES, Phase
from forge.errors import InvalidTransition
from forge.runtime.machine import (
    TRANSITIONS,
    assert_transition,
    is_terminal,
    paths_into,
    reachable_from,
)

ALL_PHASES = list(Phase)


def _all_paths(start: Phase, limit: int = 12) -> list[list[Phase]]:
    """Enumerate simple-ish paths from `start`, bounded to keep it finite."""
    paths: list[list[Phase]] = []

    def walk(path: list[Phase]) -> None:
        if len(path) > limit:
            return
        current = path[-1]
        if is_terminal(current):
            paths.append(list(path))
            return
        for nxt in sorted(TRANSITIONS.get(current, frozenset())):
            # Allow one revisit so repair/retry loops are covered, no more.
            if path.count(nxt) >= 2:
                continue
            walk([*path, nxt])

    walk([start])
    return paths


def test_every_phase_is_in_the_table() -> None:
    assert set(TRANSITIONS) == set(ALL_PHASES)


def test_terminal_phases_have_no_successors() -> None:
    for phase in TERMINAL_PHASES:
        assert TRANSITIONS[phase] == frozenset(), f"{phase} must be terminal"


def test_dispatch_is_only_reachable_through_authorize() -> None:
    """The core safety property: nothing runs without an authorization decision."""
    assert paths_into(Phase.DISPATCH) == {Phase.AUTHORIZE, Phase.RECONCILE}
    # RECONCILE -> DISPATCH is the retry edge, and a retry re-enters DISPATCH
    # only for an action that AUTHORIZE already approved in this same step.


def test_observe_is_only_reachable_through_dispatch() -> None:
    assert paths_into(Phase.OBSERVE) == {Phase.DISPATCH}


def test_commit_is_only_reachable_through_reconcile() -> None:
    assert paths_into(Phase.COMMIT) == {Phase.RECONCILE}


def test_no_path_reaches_commit_without_authorize() -> None:
    """Exhaustive search: every route to COMMIT passes through AUTHORIZE."""
    routes = [p for p in _all_paths(Phase.BOOT) if Phase.COMMIT in p]
    assert routes, "expected at least one committing path"
    for path in routes:
        assert Phase.AUTHORIZE in path, f"path reached COMMIT unauthorized: {path}"
        assert path.index(Phase.AUTHORIZE) < path.index(Phase.COMMIT)


def test_no_path_reaches_observe_without_dispatch() -> None:
    for path in _all_paths(Phase.BOOT):
        if Phase.OBSERVE in path:
            assert Phase.DISPATCH in path
            assert path.index(Phase.DISPATCH) < path.index(Phase.OBSERVE)


def test_every_phase_can_reach_a_terminal() -> None:
    """No phase is a dead end - a run always has a way to finish."""
    for phase in ALL_PHASES:
        if is_terminal(phase):
            continue
        assert reachable_from(phase) & TERMINAL_PHASES, f"{phase} cannot terminate"


def test_boot_reaches_every_non_boot_phase() -> None:
    reachable = reachable_from(Phase.BOOT)
    unreachable = set(ALL_PHASES) - reachable - {Phase.BOOT}
    assert not unreachable, f"unreachable phases: {sorted(unreachable)}"


@given(
    current=st.sampled_from(ALL_PHASES),
    nxt=st.sampled_from(ALL_PHASES),
)
@settings(max_examples=400, deadline=None)
def test_assert_transition_matches_the_table(current: Phase, nxt: Phase) -> None:
    """`assert_transition` accepts exactly the edges in TRANSITIONS - no more."""
    legal = nxt in TRANSITIONS[current]
    if legal:
        assert_transition(current, nxt)
    else:
        with pytest.raises(InvalidTransition):
            assert_transition(current, nxt)


@given(phase=st.sampled_from(list(TERMINAL_PHASES)), nxt=st.sampled_from(ALL_PHASES))
@settings(max_examples=100, deadline=None)
def test_nothing_leaves_a_terminal_phase(phase: Phase, nxt: Phase) -> None:
    with pytest.raises(InvalidTransition):
        assert_transition(phase, nxt)
