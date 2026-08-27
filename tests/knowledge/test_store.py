"""Durability: knowledge events in the real log, under the real unique index."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from forge.core.enums import EventType
from forge.core.events import NewEvent
from forge.knowledge import events as kev
from forge.knowledge.models import Note
from forge.knowledge.policy import PromotionPolicy
from forge.knowledge.projection import project
from forge.knowledge.store import SQLiteKnowledgeLog
from forge.state.sqlite_store import SQLiteEventStore


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _note(run_id: str, body: str, note_id: str) -> Note:
    return Note(
        id=note_id,
        kind="LANDMARK",
        scope="repo",
        body=body,
        anchors=(),
        author_run_id=run_id,
    )


async def _open(tmp_path: Path) -> tuple[SQLiteEventStore, SQLiteKnowledgeLog]:
    store = SQLiteEventStore(tmp_path / "events.db")
    await store.open()
    log = SQLiteKnowledgeLog(store, path=tmp_path / "events.db")
    await log.open()
    return store, log


def test_knowledge_events_land_in_the_main_log(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, log = await _open(tmp_path)
        try:
            await log.append(kev.make_note_proposed(_note("run_a", "a fact", "note_1")))
            # The runtime's own per-run read sees it: one log, not two.
            events = await store.read("run_a")
            assert [e.type for e in events] == [EventType.NOTE_PROPOSED]
        finally:
            await log.close()
            await store.close()

    run(scenario())


def test_the_cross_run_read_spans_every_run(tmp_path: Path) -> None:
    """The query the runtime does not have and the knowledge layer needs."""

    async def scenario() -> None:
        store, log = await _open(tmp_path)
        try:
            for i in range(4):
                await log.append(
                    kev.make_note_proposed(_note(f"run_{i}", f"fact {i}", f"note_{i}"))
                )
            found = await log.read_knowledge()
            assert len(found) == 4
            assert {e.run_id for e in found} == {f"run_{i}" for i in range(4)}
            assert [e.seq for e in found] == sorted(e.seq for e in found)
        finally:
            await log.close()
            await store.close()

    run(scenario())


def test_non_knowledge_events_are_excluded(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, log = await _open(tmp_path)
        try:
            await store.append(
                NewEvent(type=EventType.RUN_COMPLETED, run_id="run_a", payload={})
            )
            await log.append(kev.make_note_proposed(_note("run_a", "a fact", "note_1")))
            found = await log.read_knowledge()
            assert [e.type for e in found] == [EventType.NOTE_PROPOSED]
        finally:
            await log.close()
            await store.close()

    run(scenario())


def test_the_unique_index_dedupes_a_retried_attestation(tmp_path: Path) -> None:
    """Recording is the claim: one durable append, enforced by SQLite.

    Not an application-level check - two workers racing cannot both win.
    """

    async def scenario() -> None:
        store, log = await _open(tmp_path)
        try:
            await log.append(kev.make_note_proposed(_note("run_a", "a fact", "note_1")))
            terminal = await store.append(
                NewEvent(type=EventType.RUN_COMPLETED, run_id="run_b", payload={})
            )

            from forge.knowledge.models import Attestation
            from forge.knowledge.outcomes import OutcomeClass

            att = Attestation(
                note_id="note_1",
                run_id="run_b",
                event_seq=terminal.event.seq,
                verdict="SUPPORT",
                outcome=OutcomeClass.PASSING,
                outcome_event="RUN_COMPLETED",
            )

            results = [await log.append(kev.make_attestation_recorded(att)) for _ in range(5)]
            assert results[0].deduplicated is False
            assert all(r.deduplicated for r in results[1:])
            assert len({r.event.seq for r in results}) == 1

            found = await log.read_knowledge()
            assert sum(1 for e in found if e.type == EventType.ATTESTATION_RECORDED) == 1
        finally:
            await log.close()
            await store.close()

    run(scenario())


def test_concurrent_identical_attestations_produce_one_row(tmp_path: Path) -> None:
    """The race the unique index exists for."""

    async def scenario() -> None:
        store, log = await _open(tmp_path)
        try:
            await log.append(kev.make_note_proposed(_note("run_a", "a fact", "note_1")))
            terminal = await store.append(
                NewEvent(type=EventType.RUN_COMPLETED, run_id="run_b", payload={})
            )

            from forge.knowledge.models import Attestation
            from forge.knowledge.outcomes import OutcomeClass

            att = Attestation(
                note_id="note_1",
                run_id="run_b",
                event_seq=terminal.event.seq,
                verdict="SUPPORT",
                outcome=OutcomeClass.PASSING,
                outcome_event="RUN_COMPLETED",
            )
            results = await asyncio.gather(
                *(log.append(kev.make_attestation_recorded(att)) for _ in range(8))
            )
            assert sum(1 for r in results if not r.deduplicated) == 1
        finally:
            await log.close()
            await store.close()

    run(scenario())


def test_different_authors_writing_the_same_body_both_persist(tmp_path: Path) -> None:
    """The store cannot collapse them - its index is per run.

    Four rows land, and the projection folds them into one note. This is the
    concrete reason collapse lives in the fold rather than the store.
    """

    async def scenario() -> None:
        store, log = await _open(tmp_path)
        try:
            for i in range(4):
                await log.append(
                    kev.make_note_proposed(_note(f"run_{i}", "identical body", f"note_{i}"))
                )
            found = await log.read_knowledge()
            assert len(found) == 4, "the store keeps all four"

            state = project(found, {}, PromotionPolicy())
            assert len(state.notes) == 1, "the projection folds them into one"
        finally:
            await log.close()
            await store.close()

    run(scenario())


def test_the_projection_survives_a_round_trip_through_sqlite(tmp_path: Path) -> None:
    """JSON encoding must not change a single verdict.

    Anchors carry a tuple region that becomes a list in JSON; if the decoder
    got that wrong, staleness would silently invert.
    """

    async def scenario() -> None:
        from forge.knowledge.anchors import anchor_for
        from tests.knowledge.conftest import SOURCE_V1, SOURCE_V2

        store, log = await _open(tmp_path)
        try:
            anchor = anchor_for("forge/retry.py", SOURCE_V1, (4, 6))
            assert anchor is not None
            note = Note(
                id="note_anchored",
                kind="HAZARD",
                scope="forge/retry.py",
                body="the backoff is unbounded",
                anchors=(anchor,),
                author_run_id="run_a",
            )
            await log.append(kev.make_note_proposed(note))
            found = await log.read_knowledge()

            fresh = project(found, {"forge/retry.py": SOURCE_V1}, PromotionPolicy())
            assert fresh.status("note_anchored") == "CLAIM"

            changed = project(found, {"forge/retry.py": SOURCE_V2}, PromotionPolicy())
            assert changed.status("note_anchored") == "STALE"
        finally:
            await log.close()
            await store.close()

    run(scenario())
