"""Two defects found by auditing the layer against the rest of the runtime.

Both were real, both were reproduced before being fixed, and both defeated the
guarantee the knowledge layer exists to provide. They live in their own file
because they are regressions against *integration* with the runtime, not
against the fold.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from forge.core.enums import KNOWLEDGE_EVENT_TYPES, EventType
from forge.core.events import NewEvent
from forge.knowledge.policy import PromotionPolicy
from forge.knowledge.projection import project
from forge.knowledge.store import InMemoryKnowledgeLog, SQLiteKnowledgeLog
from forge.knowledge.tools import (
    KnowledgeSession,
    bind_session,
    knowledge_attest,
    knowledge_write,
)
from forge.state.sqlite_store import SQLiteEventStore


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# -- concurrent run identity ------------------------------------------------


def test_concurrent_runs_do_not_share_a_session() -> None:
    """Interleaved runs must each write under their own identity.

    The API runs concurrent runs as asyncio tasks in one process. With a module
    global this failed: whichever run bound last owned every subsequent write,
    so a note carried an `author_run_id` belonging to a different run - and the
    author exclusion, the whole basis of the disjointness rule, was then
    computed against an identity that never wrote it.
    """
    log = InMemoryKnowledgeLog()

    async def scenario() -> None:
        async def write_as(run_id: str, body: str, delay: float) -> None:
            bind_session(KnowledgeSession(log=log, run_id=run_id))
            await asyncio.sleep(delay)  # any real tool await yields here
            await knowledge_write(kind="LANDMARK", body=body)

        await asyncio.gather(
            write_as("run_ALPHA", "alpha insight", 0.02),
            write_as("run_BETA", "beta insight", 0.01),
        )

        events = await log.read_knowledge()
        attribution = {e.payload["body"]: e.payload["author_run_id"] for e in events}
        assert attribution == {
            "alpha insight": "run_ALPHA",
            "beta insight": "run_BETA",
        }

    run(scenario())
    bind_session(None)


def test_a_session_does_not_leak_out_of_its_task() -> None:
    """Binding inside a task must not change the caller's binding."""
    log = InMemoryKnowledgeLog()

    async def scenario() -> None:
        bind_session(KnowledgeSession(log=log, run_id="run_outer"))

        async def inner() -> None:
            bind_session(KnowledgeSession(log=log, run_id="run_inner"))
            await knowledge_write(kind="LANDMARK", body="from the inner task")

        await asyncio.create_task(inner())
        await knowledge_write(kind="LANDMARK", body="from the outer task")

        events = await log.read_knowledge()
        attribution = {e.payload["body"]: e.payload["author_run_id"] for e in events}
        assert attribution["from the inner task"] == "run_inner"
        assert attribution["from the outer task"] == "run_outer", (
            "the inner task's binding leaked into its parent"
        )

    run(scenario())
    bind_session(None)


# -- retention ---------------------------------------------------------------


def test_pruning_old_runs_preserves_their_knowledge(tmp_path: Path) -> None:
    """Retention deletes runs. It must not delete what they found out.

    A run's step history is state belonging to the run; a note it established
    is not. Before this fix, a routine `prune()` silently destroyed the shared
    knowledge base - a CORROBORATED note and both attestations backing it
    vanished, and nothing reported that anything had been lost.
    """
    db = tmp_path / "forge.db"

    async def scenario() -> None:
        store = SQLiteEventStore(db)
        await store.open()
        log = SQLiteKnowledgeLog(store, path=db)
        await log.open()
        try:
            bind_session(KnowledgeSession(log=log, run_id="run_author"))
            written = await knowledge_write(kind="LANDMARK", body="hard-won insight")
            note_id = str(written.evidence["note_id"])
            await store.append(
                NewEvent(type=EventType.RUN_CREATED, run_id="run_author", payload={})
            )
            await store.append(
                NewEvent(type=EventType.RUN_COMPLETED, run_id="run_author", payload={})
            )

            for name in ("run_p", "run_q"):
                terminal = await store.append(
                    NewEvent(type=EventType.RUN_COMPLETED, run_id=name, payload={})
                )
                bind_session(KnowledgeSession(log=log, run_id=name))
                await knowledge_attest(note_id, terminal.event.seq, "SUPPORT")

            before = project(await log.read_knowledge(), {}, PromotionPolicy())
            assert before.status(note_id) == "CORROBORATED"
            assert before.notes[note_id].independent_support == 2

            # Age the whole database, then run retention as a cron job would.
            conn = sqlite3.connect(db)
            conn.execute("UPDATE events SET ts = '2020-01-01T00:00:00+00:00'")
            conn.commit()
            conn.close()

            removed = await store.prune(older_than_days=1)
            assert removed > 0, "the fixture must actually prune something"

            after = project(await log.read_knowledge(), {}, PromotionPolicy())
            assert after.status(note_id) == "CORROBORATED"
            assert after.notes[note_id].independent_support == 2

            # The run's own history is gone, which is what prune is for.
            assert await store.read("run_author") != []
            surviving = {e.type for e in await store.read("run_author")}
            assert surviving <= KNOWLEDGE_EVENT_TYPES
            assert EventType.RUN_COMPLETED not in surviving
        finally:
            bind_session(None)
            await log.close()
            await store.close()

    run(scenario())


def test_a_pruned_run_does_not_look_resumable(tmp_path: Path) -> None:
    """Surviving knowledge events must not masquerade as abandoned work.

    `unfinished_runs` is how the supervisor finds work a dead worker left
    behind. A run whose RUN_CREATED was pruned but whose note remains has no
    RUN_CREATED, so it cannot be mistaken for something to resume.
    """
    db = tmp_path / "forge.db"

    async def scenario() -> None:
        store = SQLiteEventStore(db)
        await store.open()
        log = SQLiteKnowledgeLog(store, path=db)
        await log.open()
        try:
            bind_session(KnowledgeSession(log=log, run_id="run_author"))
            await knowledge_write(kind="LANDMARK", body="an insight")
            await store.append(
                NewEvent(type=EventType.RUN_CREATED, run_id="run_author", payload={})
            )
            await store.append(
                NewEvent(type=EventType.RUN_COMPLETED, run_id="run_author", payload={})
            )

            conn = sqlite3.connect(db)
            conn.execute("UPDATE events SET ts = '2020-01-01T00:00:00+00:00'")
            conn.commit()
            conn.close()

            await store.prune(older_than_days=1)
            assert await store.unfinished_runs() == []
        finally:
            bind_session(None)
            await log.close()
            await store.close()

    run(scenario())
