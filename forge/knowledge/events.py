"""Knowledge events: builders, decoders, and the keys that make them idempotent.

These are ordinary events in the ordinary log. They carry the same
`idempotency_key` the runtime uses for effects, so a retried step that
re-submits the same insight loses the append and the count does not move.

One thing to be clear about, because it changes the design. The store's unique
index is `(run_id, idempotency_key)` - **per run**, not global. Both keys below
embed the authoring run, so retry-dedupe works. What it cannot do is collapse
*different* runs writing byte-identical bodies: four runs produce four keys in
four scopes and four rows land. That collapse happens in the projection, keyed
on `body_hash`, which is strictly better anyway - it is reconstructible, and it
survives a policy change without a migration.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from forge.core.enums import KNOWLEDGE_EVENT_TYPES as _CORE_KNOWLEDGE_EVENT_TYPES
from forge.core.enums import EventType
from forge.core.events import Event, NewEvent
from forge.ids import content_hash
from forge.knowledge.models import (
    Anchor,
    Attestation,
    Note,
    NoteId,
    NoteKind,
    Region,
    RunId,
    Verdict,
)
from forge.knowledge.outcomes import OutcomeClass

__all__ = [
    "KNOWLEDGE_EVENT_TYPES",
    "attestation_idempotency_key",
    "body_hash",
    "decode_anchors",
    "decode_attestation",
    "decode_note",
    "encode_anchors",
    "make_adversarial_retest",
    "make_attestation_recorded",
    "make_attestation_retracted",
    "make_note_proposed",
    "make_note_quarantined",
    "make_note_reanchored",
    "make_note_released",
    "make_notes_merged",
    "note_idempotency_key",
]

# Re-exported from core so `forge.state` can honour it during retention
# without importing the knowledge package. One definition, two consumers.
KNOWLEDGE_EVENT_TYPES = _CORE_KNOWLEDGE_EVENT_TYPES


# -- encoding ---------------------------------------------------------------


def _encode_region(region: Region) -> Any:
    return region if isinstance(region, str) else list(region)


def _decode_region(raw: Any) -> Region:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return (int(raw[0]), int(raw[1]))
    raise ValueError(f"malformed anchor region: {raw!r}")


def encode_anchors(anchors: Iterable[Anchor]) -> list[dict[str, Any]]:
    return [
        {"path": a.path, "region": _encode_region(a.region), "content_hash": a.content_hash}
        for a in anchors
    ]


def decode_anchors(raw: Any) -> tuple[Anchor, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[Anchor] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            Anchor(
                path=str(item.get("path", "")),
                region=_decode_region(item.get("region")),
                content_hash=str(item.get("content_hash", "")),
            )
        )
    return tuple(out)


# -- keys -------------------------------------------------------------------


def body_hash(kind: NoteKind, scope: str, body: str, anchors: Sequence[Anchor]) -> str:
    """Identity of a note's *content*, independent of who wrote it.

    This is what collapses four runs writing the same sentence into one note.
    Anchors participate: the same words about a different region are a
    different claim.
    """
    return content_hash(kind, scope, body.strip(), encode_anchors(anchors))


def note_idempotency_key(author_run_id: RunId, body: str, anchors: Sequence[Anchor]) -> str:
    """Per-run write key: one identical note per authoring run."""
    return content_hash(author_run_id, body.strip(), encode_anchors(anchors))[:32]


def attestation_idempotency_key(
    note_id: NoteId, run_id: RunId, event_seq: int, verdict: Verdict
) -> str:
    """Per-run attestation key.

    A retried step recomputes this identically, so the second append is a
    dedupe hit and `independent_support` does not move.
    """
    return content_hash(note_id, run_id, event_seq, verdict)[:32]


# -- builders ---------------------------------------------------------------


def make_note_proposed(note: Note, *, step_id: str | None = None) -> NewEvent:
    anchors = encode_anchors(note.anchors)
    return NewEvent(
        type=EventType.NOTE_PROPOSED,
        run_id=note.author_run_id,
        step_id=step_id,
        payload={
            "note_id": note.id,
            "kind": note.kind,
            "scope": note.scope,
            "body": note.body,
            "anchors": anchors,
            "author_run_id": note.author_run_id,
            "derived_from": list(note.derived_from),
            "body_hash": body_hash(note.kind, note.scope, note.body, note.anchors),
        },
        idempotency_key=note_idempotency_key(note.author_run_id, note.body, note.anchors),
    )


def make_attestation_recorded(att: Attestation, *, step_id: str | None = None) -> NewEvent:
    return NewEvent(
        type=EventType.ATTESTATION_RECORDED,
        run_id=att.run_id,
        step_id=step_id,
        payload={
            "note_id": att.note_id,
            "run_id": att.run_id,
            "event_seq": att.event_seq,
            "verdict": att.verdict,
            "outcome": str(att.outcome),
            "outcome_event": att.outcome_event,
            "reader_lineage": list(att.reader_lineage),
        },
        idempotency_key=attestation_idempotency_key(
            att.note_id, att.run_id, att.event_seq, att.verdict
        ),
    )


def make_attestation_retracted(
    *, note_id: NoteId, run_id: RunId, event_seq: int, verdict: Verdict, actor: RunId, reason: str
) -> NewEvent:
    """Compensation for `knowledge.attest`.

    Retraction does not delete the attestation - nothing deletes from an
    append-only log. The projection stops counting it, and the retraction
    stays visible, which is the point.
    """
    return NewEvent(
        type=EventType.ATTESTATION_RETRACTED,
        run_id=actor,
        payload={
            "note_id": note_id,
            "run_id": run_id,
            "event_seq": event_seq,
            "verdict": verdict,
            "actor": actor,
            "reason": reason,
        },
        idempotency_key=content_hash("retract", note_id, run_id, event_seq, verdict)[:32],
    )


def make_note_quarantined(*, note_id: NoteId, actor: RunId, reason: str) -> NewEvent:
    return NewEvent(
        type=EventType.NOTE_QUARANTINED,
        run_id=actor,
        payload={"note_id": note_id, "actor": actor, "reason": reason},
        idempotency_key=content_hash("quarantine", note_id, actor, reason)[:32],
    )


def make_note_released(*, note_id: NoteId, actor: RunId, reason: str) -> NewEvent:
    return NewEvent(
        type=EventType.NOTE_RELEASED,
        run_id=actor,
        payload={"note_id": note_id, "actor": actor, "reason": reason},
        idempotency_key=content_hash("release", note_id, actor, reason)[:32],
    )


def make_notes_merged(*, surviving_id: NoteId, absorbed_id: NoteId, actor: RunId) -> NewEvent:
    return NewEvent(
        type=EventType.NOTES_MERGED,
        run_id=actor,
        payload={
            "surviving_id": surviving_id,
            "absorbed_id": absorbed_id,
            "actor": actor,
        },
        idempotency_key=content_hash("merge", surviving_id, absorbed_id)[:32],
    )


def make_note_reanchored(
    *, note_id: NoteId, anchors: Sequence[Anchor], observed_at: str, actor: RunId
) -> NewEvent:
    """Re-pin a stale note to the region as it now stands.

    This is the only way a STALE note re-enters circulation. New attestations
    alone cannot do it - see the resurrection test.
    """
    return NewEvent(
        type=EventType.NOTE_REANCHORED,
        run_id=actor,
        payload={
            "note_id": note_id,
            "anchors": encode_anchors(anchors),
            "observed_at": observed_at,
            "actor": actor,
        },
        idempotency_key=content_hash("reanchor", note_id, encode_anchors(anchors))[:32],
    )


def make_adversarial_retest(
    *, note_id: NoteId, passed: bool, actor: RunId, detail: str = ""
) -> NewEvent:
    """The one CANONICAL input that is not an attestation.

    Recorded by a Librarian. The projection ignores a retest whose actor
    authored the note, so this cannot be used to self-promote even by a run
    that holds the capability.
    """
    return NewEvent(
        type=EventType.ADVERSARIAL_RETEST_RECORDED,
        run_id=actor,
        payload={
            "note_id": note_id,
            "passed": passed,
            "actor": actor,
            "detail": detail,
        },
        idempotency_key=content_hash("retest", note_id, actor, passed)[:32],
    )


# -- decoding ---------------------------------------------------------------


def decode_note(event: Event) -> Note | None:
    """Rebuild a Note from a NOTE_PROPOSED event, or None if malformed.

    Malformed events are skipped rather than raised on: the fold is total, so
    a newer writer cannot crash an older reader.
    """
    p = event.payload
    note_id = p.get("note_id")
    kind = p.get("kind")
    if not isinstance(note_id, str) or not isinstance(kind, str):
        return None
    try:
        anchors = decode_anchors(p.get("anchors"))
    except ValueError:
        return None
    derived = p.get("derived_from") or []
    return Note(
        id=note_id,
        kind=kind,  # type: ignore[arg-type]  # validated at write time
        scope=str(p.get("scope", "repo")),
        body=str(p.get("body", "")),
        anchors=anchors,
        author_run_id=str(p.get("author_run_id", event.run_id)),
        derived_from=tuple(str(d) for d in derived if isinstance(d, str)),
    )


def decode_attestation(event: Event) -> Attestation | None:
    p = event.payload
    note_id = p.get("note_id")
    verdict = p.get("verdict")
    if not isinstance(note_id, str) or verdict not in ("SUPPORT", "REFUTE"):
        return None
    try:
        outcome = OutcomeClass(str(p.get("outcome")))
    except ValueError:
        return None
    lineage = p.get("reader_lineage") or []
    return Attestation(
        note_id=note_id,
        run_id=str(p.get("run_id", event.run_id)),
        event_seq=int(p.get("event_seq", 0)),
        verdict=verdict,
        outcome=outcome,
        outcome_event=str(p.get("outcome_event", "")),
        reader_lineage=tuple(str(n) for n in lineage if isinstance(n, str)),
    )
