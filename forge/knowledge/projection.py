"""Fold knowledge events into note statuses.

This module imports nothing from `store.py` and performs no I/O. That is not
tidiness: it is what lets the entire history be re-evaluated under a changed
`PromotionPolicy` without replaying anything but the log, and what makes the
cached projection genuinely disposable (ADR-0001, ADR-0007).

The fold is total. An unrecognised event advances the watermark and is
otherwise ignored, and a malformed payload is recorded in `rejected` rather
than raised on, so a newer writer cannot crash an older reader.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from forge.core.enums import EventType
from forge.core.events import Event
from forge.knowledge.anchors import TreeSnapshot, stale_anchors
from forge.knowledge.events import body_hash, decode_anchors, decode_attestation, decode_note
from forge.knowledge.lineage import LineageGraph
from forge.knowledge.models import (
    Anchor,
    Attestation,
    DiscountedAttestation,
    KnowledgeState,
    Note,
    NoteId,
    NoteState,
    NoteStatus,
    RunId,
)
from forge.knowledge.policy import PromotionPolicy, discount_reason

__all__ = ["project"]


class _Raw:
    """Intermediate accumulation. Not exported; `project` owns its lifetime."""

    __slots__ = (
        "anchors",
        "attestations",
        "merges",
        "notes",
        "order",
        "quarantined",
        "retests",
        "retracted",
    )

    def __init__(self) -> None:
        self.notes: dict[NoteId, Note] = {}
        self.order: dict[NoteId, int] = {}
        self.anchors: dict[NoteId, tuple[Anchor, ...]] = {}
        self.attestations: list[tuple[int, Attestation]] = []
        self.retracted: set[tuple[NoteId, RunId, int, str]] = set()
        self.quarantined: dict[NoteId, tuple[int, bool]] = {}
        self.merges: list[tuple[NoteId, NoteId]] = []
        self.retests: dict[NoteId, tuple[int, bool, RunId]] = {}


def _accumulate(events: Iterable[Event], state: KnowledgeState) -> _Raw:
    raw = _Raw()
    for ev in events:
        state.last_seq = max(state.last_seq, ev.seq)
        p = ev.payload

        match ev.type:
            case EventType.NOTE_PROPOSED:
                note = decode_note(ev)
                if note is None:
                    state.rejected.append(("UNKNOWN_NOTE", f"seq={ev.seq} malformed note"))
                    continue
                if note.id in raw.notes:
                    continue  # first write wins; a replay changes nothing
                raw.notes[note.id] = note
                raw.order[note.id] = ev.seq
                raw.anchors[note.id] = note.anchors

            case EventType.ATTESTATION_RECORDED:
                att = decode_attestation(ev)
                if att is None:
                    state.rejected.append(
                        ("UNRESOLVED_EVENT_SEQ", f"seq={ev.seq} malformed attestation")
                    )
                    continue
                raw.attestations.append((ev.seq, att))

            case EventType.ATTESTATION_RETRACTED:
                raw.retracted.add(
                    (
                        str(p.get("note_id", "")),
                        str(p.get("run_id", "")),
                        int(p.get("event_seq", 0)),
                        str(p.get("verdict", "")),
                    )
                )

            case EventType.NOTE_QUARANTINED:
                nid = str(p.get("note_id", ""))
                prior_q = raw.quarantined.get(nid)
                # Guard on seq, not on arrival order. Without this the fold is
                # order-dependent: a release that arrives first in the iterable
                # but *earlier* in the log would be overwritten by a quarantine
                # it actually precedes, and two readers of the same log could
                # disagree about whether a note is in circulation.
                if prior_q is None or ev.seq >= prior_q[0]:
                    raw.quarantined[nid] = (ev.seq, True)

            case EventType.NOTE_RELEASED:
                nid = str(p.get("note_id", ""))
                prior = raw.quarantined.get(nid)
                if prior is None or ev.seq >= prior[0]:
                    raw.quarantined[nid] = (ev.seq, False)

            case EventType.NOTES_MERGED:
                raw.merges.append(
                    (str(p.get("absorbed_id", "")), str(p.get("surviving_id", "")))
                )

            case EventType.NOTE_REANCHORED:
                nid = str(p.get("note_id", ""))
                try:
                    raw.anchors[nid] = decode_anchors(p.get("anchors"))
                except ValueError:
                    state.rejected.append(("UNKNOWN_NOTE", f"seq={ev.seq} bad reanchor"))

            case EventType.ADVERSARIAL_RETEST_RECORDED:
                nid = str(p.get("note_id", ""))
                prior_retest = raw.retests.get(nid)
                if prior_retest is None or ev.seq >= prior_retest[0]:
                    raw.retests[nid] = (
                        ev.seq,
                        bool(p.get("passed", False)),
                        str(p.get("actor", ev.run_id)),
                    )

            case _:
                pass  # forward-compatible: unknown events only advance the seq

    return raw


def _collapse_identical(raw: _Raw, graph: LineageGraph) -> None:
    """Fold byte-identical notes from different runs into one.

    The store cannot do this: its unique index is per-run, so four runs writing
    the same sentence produce four rows. Collapsing here is better anyway -
    it is reconstructible, and the survivor is elected deterministically by
    lowest sequence number, so every reader agrees without coordination.
    """
    by_content: dict[str, list[NoteId]] = {}
    for note_id, note in raw.notes.items():
        digest = body_hash(note.kind, note.scope, note.body, note.anchors)
        by_content.setdefault(digest, []).append(note_id)

    for ids in by_content.values():
        if len(ids) < 2:
            continue
        survivor = min(ids, key=lambda n: raw.order[n])
        for other in ids:
            if other != survivor:
                graph.add_merge(other, survivor)


def _tally(
    note: Note,
    attestations: Sequence[tuple[int, Attestation]],
    graph: LineageGraph,
    raw: _Raw,
    policy: PromotionPolicy,
    authors: frozenset[RunId],
) -> tuple[list[RunId], list[RunId], list[DiscountedAttestation]]:
    """Count independent support and refutation for one note.

    Deterministic: attestations are processed in sequence order, so the run
    that gets the vote when a run attests twice is always the earlier one.
    """
    support: list[RunId] = []
    refute: list[RunId] = []
    discounted: list[DiscountedAttestation] = []
    counted: set[RunId] = set()

    for _seq, att in sorted(attestations, key=lambda pair: pair[0]):
        retracted = (
            att.note_id,
            att.run_id,
            att.event_seq,
            att.verdict,
        ) in raw.retracted

        reason = discount_reason(
            att,
            note,
            graph,
            authors=authors,
            counted_runs=frozenset(counted),
            retracted=retracted,
            policy=policy,
        )
        if reason is not None:
            discounted.append(DiscountedAttestation(attestation=att, reason=reason))
            continue

        # One vote per run per note, whichever verdict came first. A run that
        # attests both ways is contradicting itself, not voting twice.
        counted.add(att.run_id)
        if att.verdict == "SUPPORT":
            support.append(att.run_id)
        else:
            refute.append(att.run_id)

    return support, refute, discounted


def _classify(
    *,
    quarantined: bool,
    stale: tuple[Anchor, ...],
    support: int,
    refute: int,
    retest_passed: bool,
    policy: PromotionPolicy,
) -> tuple[NoteStatus, str]:
    """The promotion predicate, in precedence order."""
    if quarantined and policy.quarantine_dominates_staleness:
        return "QUARANTINED", "quarantined by a librarian and not released"
    if stale:
        paths = ", ".join(sorted({a.path for a in stale}))
        return "STALE", f"anchored region changed: {paths}"
    if quarantined:
        return "QUARANTINED", "quarantined by a librarian and not released"
    if refute > support:
        return "REFUTED", f"independent refutation ({refute}) exceeds support ({support})"
    if (
        support >= policy.canonical_support
        and refute == 0
        and (retest_passed or not policy.require_adversarial_retest)
    ):
        return "CANONICAL", f"{support} independent supporting runs, adversarial retest passed"
    if support >= policy.corroborated_support:
        if support >= policy.canonical_support and policy.require_adversarial_retest:
            return (
                "CORROBORATED",
                f"{support} independent supporting runs; awaiting adversarial retest",
            )
        return "CORROBORATED", f"{support} independent supporting runs"
    return "CLAIM", f"{support} independent supporting run(s); needs {policy.corroborated_support}"


def project(
    events: Iterable[Event],
    tree_snapshot: TreeSnapshot | None = None,
    policy: PromotionPolicy | None = None,
    now: datetime | None = None,
) -> KnowledgeState:
    """Fold knowledge events into note statuses.

    `now` is accepted and deliberately unused by the current predicate. That is
    the point of content-hash anchoring: a repository fact does not decay with
    age, it decays when the code it describes changes. The parameter stays in
    the signature because a future policy may want wall-clock (a MODEL_FACT
    about a model version reasonably expires), and changing the signature later
    would churn every caller.
    """
    del now  # see docstring

    policy = policy or PromotionPolicy()
    tree: TreeSnapshot = tree_snapshot if tree_snapshot is not None else {}
    state = KnowledgeState()

    raw = _accumulate(events, state)

    graph = LineageGraph()
    for note_id, note in raw.notes.items():
        graph.add_derivation(note_id, note.derived_from)
    for absorbed, survivor in raw.merges:
        if absorbed in raw.notes and survivor in raw.notes:
            graph.add_merge(absorbed, survivor)
    _collapse_identical(raw, graph)

    # Route every attestation to the note that currently carries the content.
    routed: dict[NoteId, list[tuple[int, Attestation]]] = {}
    for seq, att in raw.attestations:
        target = graph.survivor(att.note_id)
        if target not in raw.notes:
            state.rejected.append(("UNKNOWN_NOTE", f"attestation for unknown note {att.note_id}"))
            continue
        routed.setdefault(target, []).append((seq, att))

    for note_id, note in raw.notes.items():
        if graph.survivor(note_id) != note_id:
            continue  # absorbed; its state lives on the survivor

        absorbed_ids = graph.absorbed_into(note_id)
        anchors = raw.anchors.get(note_id, note.anchors)
        stale = stale_anchors(anchors, tree)

        quarantine = raw.quarantined.get(note_id)
        is_quarantined = bool(quarantine and quarantine[1])

        retest = raw.retests.get(note_id)
        # A retest recorded by the note's own author proves nothing. This is a
        # second, independent barrier to self-promotion: even a run holding the
        # librarian capability cannot canonise what it wrote.
        retest_passed = bool(retest and retest[1] and retest[2] != note.author_run_id)

        # Every run whose identical body folded into this note authored it.
        authors = frozenset(
            {note.author_run_id}
            | {raw.notes[a].author_run_id for a in absorbed_ids if a in raw.notes}
        )

        attestations = routed.get(note_id, [])
        support, refute, discounted = _tally(note, attestations, graph, raw, policy, authors)

        status, reason = _classify(
            quarantined=is_quarantined,
            stale=stale,
            support=len(support),
            refute=len(refute),
            retest_passed=retest_passed,
            policy=policy,
        )

        state.notes[note_id] = NoteState(
            note=note,
            status=status,
            reason=reason,
            independent_support=len(support),
            independent_refute=len(refute),
            supporting_runs=tuple(support),
            refuting_runs=tuple(refute),
            discounted=tuple(discounted),
            absorbed=tuple(sorted(absorbed_ids)),
            stale_anchors=stale,
            quarantined=is_quarantined,
            retest_passed=retest_passed,
            attestation_count=len(attestations),
        )

    return state
