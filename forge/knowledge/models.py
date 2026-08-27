"""Knowledge data model: notes, anchors, attestations and projected state.

Data only. Every rule about what these *mean* lives in `policy.py` and
`projection.py`, because status is derived and must stay derivable - see
docs/adr/0007-knowledge-status-is-a-projection.md.

The one thing worth reading twice is `NoteState.discounted`: when an
attestation does not count toward corroboration, the reason is recorded rather
than the attestation being silently dropped. A store that quietly ignores
evidence is indistinguishable from one that is broken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from forge.knowledge.outcomes import OutcomeClass

__all__ = [
    "MAX_BODY_CHARS",
    "Anchor",
    "Attestation",
    "DiscountReason",
    "DiscountedAttestation",
    "Note",
    "NoteId",
    "NoteKind",
    "NoteState",
    "NoteStatus",
    "Region",
    "RejectReason",
    "RunId",
    "Verdict",
]

NoteId = str
RunId = str

#: A line span (1-based, inclusive) or a symbol name.
Region = tuple[int, int] | str

NoteKind = Literal["LANDMARK", "RECIPE", "HAZARD", "CONSTRAINT", "MODEL_FACT"]

NoteStatus = Literal[
    "CLAIM",
    "CORROBORATED",
    "CANONICAL",
    "STALE",
    "REFUTED",
    "QUARANTINED",
]

Verdict = Literal["SUPPORT", "REFUTE"]

#: Why an attestation did not count toward independent corroboration.
DiscountReason = Literal[
    "SELF_AUTHORED",
    "LINEAGE_CONTAMINATED",
    "DUPLICATE_RUN",
    "NON_PASSING_OUTCOME",
    "RETRACTED",
]

#: Why a write was refused outright. Rejection happens at write time so the
#: log never contains evidence that the projection would have to filter.
RejectReason = Literal[
    "BODY_TOO_LONG",
    "BODY_EMPTY",
    "UNRESOLVED_EVENT_SEQ",
    "NON_PASSING_SUPPORT",
    "UNKNOWN_NOTE",
]

MAX_BODY_CHARS = 500


@dataclass(frozen=True)
class Anchor:
    """A claim about a region of code, pinned by content rather than by time.

    `content_hash` is the hash of the region *as it was when the note was
    written*. A note is stale when the region no longer hashes to this - which
    is a fact about the repository, not a guess about age.
    """

    path: str
    region: Region
    content_hash: str


@dataclass(frozen=True)
class Note:
    id: NoteId
    kind: NoteKind
    scope: str
    body: str
    anchors: tuple[Anchor, ...]
    author_run_id: RunId
    derived_from: tuple[NoteId, ...] = ()


@dataclass(frozen=True)
class Attestation:
    """Evidence that a run's *outcome* bears on a note.

    `event_seq` must resolve to a terminal event in the log. An attestation is
    not an opinion: it is a pointer at something that already happened and can
    be re-checked by anyone reading the same log.
    """

    note_id: NoteId
    run_id: RunId
    event_seq: int
    verdict: Verdict
    outcome: OutcomeClass
    outcome_event: str
    """The EventType name that resolved, kept so the resolution is auditable."""

    reader_lineage: tuple[NoteId, ...] = ()


@dataclass(frozen=True)
class DiscountedAttestation:
    """An attestation that was seen, understood, and deliberately not counted."""

    attestation: Attestation
    reason: DiscountReason


@dataclass(frozen=True)
class NoteState:
    """The projection's verdict on one note. Never stored; always recomputed."""

    note: Note
    status: NoteStatus
    reason: str
    """Human-readable justification for `status`, for traces and tests."""

    independent_support: int = 0
    independent_refute: int = 0
    supporting_runs: tuple[RunId, ...] = ()
    refuting_runs: tuple[RunId, ...] = ()
    discounted: tuple[DiscountedAttestation, ...] = ()
    absorbed: tuple[NoteId, ...] = ()
    """Notes collapsed into this one by identical content, or merged explicitly."""

    stale_anchors: tuple[Anchor, ...] = ()
    quarantined: bool = False
    retest_passed: bool = False
    attestation_count: int = 0
    """Total attestations seen, counted or not. `attestation_count -
    len(discounted)` is not the support count: refutes are counted too."""

    def discounted_for(self, reason: DiscountReason) -> tuple[Attestation, ...]:
        """Attestations dropped for one specific reason.

        Tests assert against this rather than against `status != CANONICAL`:
        a fixture that fails to promote for the *wrong* reason is a bug the
        weaker assertion would hide.
        """
        return tuple(d.attestation for d in self.discounted if d.reason == reason)


@dataclass
class KnowledgeState:
    """The whole fold. `notes` is the answer; the rest is why."""

    notes: dict[NoteId, NoteState] = field(default_factory=dict)
    last_seq: int = 0
    rejected: list[tuple[RejectReason, str]] = field(default_factory=list)
    """Writes the projection refused to honour, for forward-compatibility: a
    newer writer's malformed event must not crash an older reader."""

    def status(self, note_id: NoteId) -> NoteStatus | None:
        state = self.notes.get(note_id)
        return state.status if state else None
