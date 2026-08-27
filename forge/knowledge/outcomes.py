"""Resolving an attestation's `event_seq` to a verifiable outcome.

An attestation is only worth anything if the outcome it points at is real and
re-checkable. That means resolving a sequence number against the log and
classifying the event it lands on.

**Why this module exists rather than reusing the harness `Outcome` enum.**
The harness does not write to the event log - `forge.eval.results` writes JSONL
and a manifest to a directory, and nothing under `forge/eval/` appends events.
So a harness `Outcome` cannot be resolved from a sequence number today. The
log's terminal vocabulary is `RUN_COMPLETED / RUN_FAILED / RUN_ABORTED`, and
that is what an attestation may cite.

The resolver is a registry rather than a match statement so the harness can be
bridged later - `register_terminal(EventType.CASE_OUTCOME_RECORDED, ...)` and
every historical attestation reclassifies on the next projection, because
status is derived and nothing was cached. No migration.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from forge.core.enums import EventType
from forge.core.events import Event

__all__ = [
    "OutcomeClass",
    "register_terminal",
    "registered_terminals",
    "resolve_terminal",
]


class OutcomeClass(StrEnum):
    """What a terminal event says about the run that produced it."""

    PASSING = "PASSING"
    FAILING = "FAILING"

    @property
    def is_passing(self) -> bool:
        return self is OutcomeClass.PASSING


# Deliberately mutable: see the module docstring. Seeded with the runtime's
# own terminals, which are the only ones the log contains today.
_TERMINALS: dict[EventType, OutcomeClass] = {
    EventType.RUN_COMPLETED: OutcomeClass.PASSING,
    EventType.RUN_FAILED: OutcomeClass.FAILING,
    EventType.RUN_ABORTED: OutcomeClass.FAILING,
}


def register_terminal(event_type: EventType, outcome: OutcomeClass) -> None:
    """Teach the resolver about another terminal event type.

    Idempotent, and safe to call at import time from an integration module.
    """
    _TERMINALS[event_type] = outcome


def registered_terminals() -> dict[EventType, OutcomeClass]:
    """A copy, so callers cannot mutate the registry by accident."""
    return dict(_TERMINALS)


def resolve_terminal(
    events: Iterable[Event], *, run_id: str, event_seq: int
) -> tuple[EventType, OutcomeClass] | None:
    """Find `event_seq` in `run_id` and classify it, or return None.

    None means "this attestation cites nothing verifiable" and is a write-time
    rejection, not a projection-time discount. An unresolvable pointer is
    malformed evidence; it never reaches the log.
    """
    for event in events:
        if event.seq != event_seq:
            continue
        if event.run_id != run_id:
            # The sequence exists but belongs to a different run: citing
            # someone else's terminal event as your own outcome is exactly
            # the confusion this check exists to catch.
            return None
        outcome = _TERMINALS.get(event.type)
        return (event.type, outcome) if outcome is not None else None
    return None
