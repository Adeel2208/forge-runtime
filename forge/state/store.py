"""The `EventStore` protocol.

Deliberately narrow, and deliberately not an ORM. The runtime needs exactly
five things from durable storage; keeping the surface this small is what makes
the Postgres backend a small delta rather than a rewrite
(see docs/adr/0004-sqlite-default-postgres-optional.md).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from forge.core.contracts import Checkpoint
from forge.core.events import Event, NewEvent

__all__ = ["AppendResult", "EventStore"]


class AppendResult:
    """Outcome of an append, distinguishing a real write from a dedupe hit."""

    __slots__ = ("deduplicated", "event")

    def __init__(self, event: Event, deduplicated: bool = False) -> None:
        self.event = event
        self.deduplicated = deduplicated

    def __repr__(self) -> str:
        flag = " deduplicated" if self.deduplicated else ""
        return f"<AppendResult seq={self.event.seq} {self.event.type}{flag}>"


@runtime_checkable
class EventStore(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def append(self, event: NewEvent) -> AppendResult:
        """Append one event atomically.

        If `event.idempotency_key` is set and already present for this run, the
        store must NOT write a second row - it returns the existing event with
        ``deduplicated=True``. This is the primitive that makes resume safe.
        """
        ...

    async def read(self, run_id: str, *, after_seq: int = 0) -> list[Event]:
        """All events for a run with ``seq > after_seq``, in order."""
        ...

    async def find_effect(self, run_id: str, idempotency_key: str) -> Event | None:
        """The recorded effect for an idempotency key, if one exists."""
        ...

    async def write_checkpoint(self, checkpoint: Checkpoint) -> None: ...

    async def latest_checkpoint(self, run_id: str) -> Checkpoint | None: ...

    async def list_runs(self, *, limit: int = 50) -> list[dict[str, object]]: ...

    async def prune(self, *, older_than_days: float, keep_unfinished: bool = True) -> int:
        """Delete finished runs older than a cutoff. Returns runs removed.

        Whole runs only. Partially pruning a run's events would leave a log
        that projects to a state that never existed - worse than no history,
        because it looks like history. Unfinished runs are retained by
        default: they may still be recoverable.
        """
        ...

    async def unfinished_runs(self, *, limit: int = 100) -> list[str]:
        """Runs that were started and never reached a terminal event.

        This is how a supervisor finds work a dead worker abandoned. It is
        derived purely from the log - a killed process cannot write a
        "I died" marker, so absence of a terminal event *is* the signal.
        """
        ...
