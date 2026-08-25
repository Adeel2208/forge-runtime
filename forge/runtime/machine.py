"""The lifecycle state machine (spec §5).

The whole safety argument of FORGE rests on this table. Because `AUTHORIZE` is
the only phase with an edge into `DISPATCH`, and `DISPATCH` the only one into
`OBSERVE`, "a tool ran without an authorization decision" is not a code-review
question - it is unreachable, and `tests/unit/test_machine_invariants.py`
proves it by exhaustive search over the graph.
"""

from __future__ import annotations

from forge.core.enums import TERMINAL_PHASES, Phase
from forge.errors import InvalidTransition

__all__ = ["TRANSITIONS", "assert_transition", "is_terminal", "paths_into", "reachable_from"]


# The complete edge set. Every entry has a reason to exist:
TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    Phase.BOOT: frozenset({Phase.VIEW, Phase.FAILED}),
    # VIEW -> PROPOSE: normal. -> FAILED: context could not be compiled.
    Phase.VIEW: frozenset({Phase.PROPOSE, Phase.FAILED}),
    # PROPOSE -> VALIDATE always; a proposal is never used unvalidated.
    # PROPOSE -> VIEW is the transient-retry edge: the model call itself failed
    # (timeout, provider outage), so there is no proposal to validate and the
    # runtime recompiles the view and asks again under backoff.
    Phase.PROPOSE: frozenset({Phase.VALIDATE, Phase.VIEW, Phase.FAILED}),
    # VALIDATE -> VIEW is the repair loop: bad JSON means recompile and reprompt.
    # VALIDATE -> EVALUATE is the ANSWER path, which needs no authorization.
    Phase.VALIDATE: frozenset({Phase.AUTHORIZE, Phase.VIEW, Phase.EVALUATE, Phase.FAILED}),
    # AUTHORIZE -> EVALUATE on denial: the run continues, having learned.
    Phase.AUTHORIZE: frozenset({Phase.DISPATCH, Phase.EVALUATE, Phase.ABORTED, Phase.FAILED}),
    Phase.DISPATCH: frozenset({Phase.OBSERVE, Phase.FAILED}),
    Phase.OBSERVE: frozenset({Phase.RECONCILE, Phase.FAILED}),
    # RECONCILE -> DISPATCH is retry/compensation for a mismatched effect.
    Phase.RECONCILE: frozenset({Phase.COMMIT, Phase.DISPATCH, Phase.FAILED}),
    Phase.COMMIT: frozenset({Phase.EVALUATE, Phase.FAILED}),
    Phase.EVALUATE: frozenset(
        {Phase.CONTINUE, Phase.COMPLETE, Phase.HANDOFF, Phase.FAILED, Phase.ABORTED}
    ),
    Phase.CONTINUE: frozenset({Phase.VIEW, Phase.COMPLETE, Phase.FAILED}),
    # Terminals.
    Phase.HANDOFF: frozenset(),
    Phase.COMPLETE: frozenset(),
    Phase.FAILED: frozenset(),
    Phase.ABORTED: frozenset(),
}


def is_terminal(phase: Phase) -> bool:
    return phase in TERMINAL_PHASES


def assert_transition(current: Phase, nxt: Phase) -> None:
    """Raise `InvalidTransition` if the edge is not in the table."""
    allowed = TRANSITIONS.get(current, frozenset())
    if nxt not in allowed:
        raise InvalidTransition(
            f"illegal lifecycle transition {current.value} -> {nxt.value}",
            allowed=sorted(p.value for p in allowed),
        )


def reachable_from(start: Phase) -> set[Phase]:
    """Transitive closure. Used by the invariant tests."""
    seen: set[Phase] = set()
    stack = [start]
    while stack:
        phase = stack.pop()
        for nxt in TRANSITIONS.get(phase, frozenset()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def paths_into(target: Phase) -> set[Phase]:
    """Every phase with a direct edge into `target`."""
    return {phase for phase, nexts in TRANSITIONS.items() if target in nexts}
