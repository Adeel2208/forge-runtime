"""Durable access to knowledge events.

Knowledge is inherently cross-run: a note written by one run is corroborated by
another. The runtime's `EventStore.read()` is scoped to a single run, which is
right for the runtime and insufficient here, so this module adds the one query
the knowledge layer needs without widening the `EventStore` protocol.

Writes go through the supplied `EventStore`, so knowledge events land in the
same log, under the same `(run_id, idempotency_key)` unique index, with the
same durability guarantees. Only the *read* is new.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from forge.core.enums import EventType
from forge.core.events import Event, NewEvent
from forge.knowledge.events import KNOWLEDGE_EVENT_TYPES
from forge.state.store import AppendResult, EventStore

__all__ = ["InMemoryKnowledgeLog", "KnowledgeLog", "SQLiteKnowledgeLog"]


@runtime_checkable
class KnowledgeLog(Protocol):
    """Append knowledge events; read them back across every run."""

    async def append(self, event: NewEvent) -> AppendResult: ...

    async def read_knowledge(self, *, after_seq: int = 0) -> list[Event]:
        """Every knowledge event in the log with ``seq > after_seq``, in order.

        Across all runs, which is the whole point.
        """
        ...

    async def read_run(self, run_id: str, *, after_seq: int = 0) -> list[Event]:
        """One run's events, for resolving an attestation's terminal outcome."""
        ...


class SQLiteKnowledgeLog:
    """Implements `KnowledgeLog` over the same database file as the runtime.

    Opens its own read connection rather than reaching into the event store's
    private one. WAL journalling plus `busy_timeout` already make concurrent
    readers safe - the store's own docstring notes that an API replica and a
    supervisor share this file.
    """

    def __init__(self, store: EventStore, *, path: str | Path) -> None:
        self._store = store
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> None:
        if self._conn is None:
            self._conn = await asyncio.to_thread(self._connect)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    # -- write -------------------------------------------------------------

    async def append(self, event: NewEvent) -> AppendResult:
        """Delegate to the canonical store: one log, one index, one truth."""
        return await self._store.append(event)

    # -- read --------------------------------------------------------------

    async def read_knowledge(self, *, after_seq: int = 0) -> list[Event]:
        async with self._lock:
            await self.open()
            return await asyncio.to_thread(self._read_sync, after_seq)

    async def read_run(self, run_id: str, *, after_seq: int = 0) -> list[Event]:
        """Delegate: the runtime's own per-run read is exactly right here."""
        return await self._store.read(run_id, after_seq=after_seq)

    def _read_sync(self, after_seq: int) -> list[Event]:
        conn = self._conn
        if conn is None:  # pragma: no cover - open() precedes every call
            raise RuntimeError("knowledge log is not open")
        names = sorted(t.value for t in KNOWLEDGE_EVENT_TYPES)
        placeholders = ",".join("?" for _ in names)
        rows = conn.execute(
            f"SELECT * FROM events WHERE seq > ? AND type IN ({placeholders}) ORDER BY seq",
            (after_seq, *names),
        ).fetchall()
        return [_row_to_event(row) for row in rows]


def _row_to_event(row: sqlite3.Row) -> Event:
    payload: dict[str, Any] = json.loads(row["payload"])
    return Event(
        seq=int(row["seq"]),
        run_id=str(row["run_id"]),
        step_id=row["step_id"],
        step_index=row["step_index"],
        type=EventType(row["type"]),
        payload=payload,
        idempotency_key=row["idempotency_key"],
        ts=datetime.fromisoformat(row["ts"]),
    )


class InMemoryKnowledgeLog:
    """A `KnowledgeLog` with no database, for tests and pure fixtures.

    Enforces the same per-run idempotency rule the SQLite index enforces, so a
    test that passes here is testing the same dedupe semantics production uses.
    """

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._keys: dict[tuple[str, str], Event] = {}
        self._seq = 0

    async def append(self, event: NewEvent) -> AppendResult:
        if event.idempotency_key is not None:
            existing = self._keys.get((event.run_id, event.idempotency_key))
            if existing is not None:
                return AppendResult(existing, deduplicated=True)

        self._seq += 1
        stored = Event(seq=self._seq, ts=datetime.now().astimezone(), **event.model_dump())
        self._events.append(stored)
        if event.idempotency_key is not None:
            self._keys[(event.run_id, event.idempotency_key)] = stored
        return AppendResult(stored, deduplicated=False)

    async def read_knowledge(self, *, after_seq: int = 0) -> list[Event]:
        return [
            e
            for e in self.all_events
            if e.seq > after_seq and e.type in KNOWLEDGE_EVENT_TYPES
        ]

    async def read_run(self, run_id: str, *, after_seq: int = 0) -> list[Event]:
        return [e for e in self.all_events if e.run_id == run_id and e.seq > after_seq]

    def extend(self, events: Iterable[Event]) -> None:
        """Seed non-knowledge events (terminal outcomes) that attestations cite."""
        for event in events:
            self._events.append(event)
            self._seq = max(self._seq, event.seq)

    @property
    def all_events(self) -> list[Event]:
        return sorted(self._events, key=lambda e: e.seq)
