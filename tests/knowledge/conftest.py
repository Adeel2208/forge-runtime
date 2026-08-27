"""Fixtures for the knowledge layer.

Everything here builds real `Event` objects with real sequence numbers, so the
tests exercise the same fold production uses. Nothing is mocked: the whole
layer is pure functions over a log, which is the point of ADR-0007.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from forge.core.enums import EventType
from forge.core.events import Event, NewEvent
from forge.ids import new_id
from forge.knowledge import events as kev
from forge.knowledge.anchors import anchor_for
from forge.knowledge.models import Anchor, Attestation, Note, NoteId, Region, RunId, Verdict
from forge.knowledge.outcomes import OutcomeClass

__all__ = ["Log", "make_note"]


class Log:
    """An append-only log that hands out sequence numbers, like the real one.

    Also enforces the store's `(run_id, idempotency_key)` uniqueness, so a
    dedupe test here is testing the same rule SQLite enforces in production.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._seq = 0
        self._keys: dict[tuple[str, str], Event] = {}

    def add(self, new: NewEvent) -> Event:
        if new.idempotency_key is not None:
            existing = self._keys.get((new.run_id, new.idempotency_key))
            if existing is not None:
                return existing  # dedupe hit: no second row
        self._seq += 1
        event = Event(seq=self._seq, ts=datetime.now(UTC), **new.model_dump())
        self.events.append(event)
        if new.idempotency_key is not None:
            self._keys[(new.run_id, new.idempotency_key)] = event
        return event

    # -- runtime terminals -------------------------------------------------

    def terminal(self, run_id: RunId, *, passing: bool = True) -> int:
        """Record a terminal event for `run_id` and return its sequence number."""
        return self.add(
            NewEvent(
                type=EventType.RUN_COMPLETED if passing else EventType.RUN_FAILED,
                run_id=run_id,
                payload={"answer": "done"} if passing else {"error": "failed"},
            )
        ).seq

    def non_terminal(self, run_id: RunId) -> int:
        """A sequence number that exists but is not a terminal outcome."""
        return self.add(
            NewEvent(type=EventType.STEP_STARTED, run_id=run_id, payload={"index": 1})
        ).seq

    # -- knowledge ---------------------------------------------------------

    def note(
        self,
        *,
        author: RunId,
        body: str = "the retry path reuses the idempotency key",
        kind: str = "LANDMARK",
        scope: str = "repo",
        anchors: tuple[Anchor, ...] = (),
        derived_from: tuple[NoteId, ...] = (),
        note_id: NoteId | None = None,
    ) -> Note:
        built = Note(
            id=note_id or new_id("note"),
            kind=kind,  # type: ignore[arg-type]
            scope=scope,
            body=body,
            anchors=anchors,
            author_run_id=author,
            derived_from=derived_from,
        )
        self.add(kev.make_note_proposed(built))
        return built

    def attest(
        self,
        note: Note | NoteId,
        *,
        run_id: RunId,
        verdict: Verdict = "SUPPORT",
        passing: bool = True,
        lineage: tuple[NoteId, ...] = (),
        event_seq: int | None = None,
    ) -> Attestation:
        """Attest, minting a terminal event for `run_id` unless one is given."""
        note_id = note.id if isinstance(note, Note) else note
        seq = event_seq if event_seq is not None else self.terminal(run_id, passing=passing)
        att = Attestation(
            note_id=note_id,
            run_id=run_id,
            event_seq=seq,
            verdict=verdict,
            outcome=OutcomeClass.PASSING if passing else OutcomeClass.FAILING,
            outcome_event=(
                EventType.RUN_COMPLETED.value if passing else EventType.RUN_FAILED.value
            ),
            reader_lineage=lineage,
        )
        self.add(kev.make_attestation_recorded(att))
        return att

    def corroborate(self, note: Note, *, runs: int, start: int = 1) -> list[RunId]:
        """`runs` independent supporting runs, none of which read the note."""
        ids = [f"run_ind_{i}" for i in range(start, start + runs)]
        for rid in ids:
            self.attest(note, run_id=rid)
        return ids

    def retest(self, note: Note, *, passed: bool = True, actor: RunId = "run_librarian") -> None:
        self.add(kev.make_adversarial_retest(note_id=note.id, passed=passed, actor=actor))

    def quarantine(self, note: Note, *, actor: RunId = "run_librarian", reason: str = "") -> None:
        self.add(kev.make_note_quarantined(note_id=note.id, actor=actor, reason=reason))

    def release(self, note: Note, *, actor: RunId = "run_librarian", reason: str = "") -> None:
        self.add(kev.make_note_released(note_id=note.id, actor=actor, reason=reason))

    def merge(self, *, surviving: NoteId, absorbed: NoteId, actor: RunId = "run_librarian") -> None:
        self.add(
            kev.make_notes_merged(surviving_id=surviving, absorbed_id=absorbed, actor=actor)
        )

    def reanchor(self, note: Note, anchors: tuple[Anchor, ...], *, actor: RunId = "run_lib") -> None:
        self.add(
            kev.make_note_reanchored(
                note_id=note.id, anchors=anchors, observed_at="now", actor=actor
            )
        )


def make_note(**kwargs: Any) -> Note:
    """A Note with sensible defaults, for tests that never touch a log."""
    defaults: dict[str, Any] = {
        "id": new_id("note"),
        "kind": "LANDMARK",
        "scope": "repo",
        "body": "a fact",
        "anchors": (),
        "author_run_id": "run_author",
        "derived_from": (),
    }
    defaults.update(kwargs)
    return Note(**defaults)


@pytest.fixture
def log() -> Log:
    return Log()


SOURCE_V1 = """\
import time


def retry(fn, attempts=3):
    for i in range(attempts):
        try:
            return fn()
        except OSError:
            time.sleep(2 ** i)
    raise RuntimeError("exhausted")
"""

SOURCE_V2 = """\
import time


def retry(fn, attempts=5):
    for i in range(attempts):
        try:
            return fn()
        except OSError:
            time.sleep(min(2 ** i, 30))
    raise RuntimeError("exhausted")
"""


@pytest.fixture
def tree() -> dict[str, str]:
    return {"forge/retry.py": SOURCE_V1}


@pytest.fixture
def changed_tree() -> dict[str, str]:
    return {"forge/retry.py": SOURCE_V2}


@pytest.fixture
def retry_anchor(tree: dict[str, str]) -> Anchor:
    built = anchor_for("forge/retry.py", tree["forge/retry.py"], "retry")
    assert built is not None
    return built


def region_anchor(tree: dict[str, str], path: str, region: Region) -> Anchor:
    built = anchor_for(path, tree[path], region)
    assert built is not None
    return built
