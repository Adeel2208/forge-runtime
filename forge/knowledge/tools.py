"""Capability-gated knowledge tools.

Two registries, not one. `AGENT_TOOLS` carries write/attest/read;
`LIBRARIAN_TOOLS` carries promotion and curation. A worker's registry does not
contain the promote symbol at all, so self-promotion is refused twice: once
because the tool is not in scope, and once because the capability is not
granted.

There is a third barrier, and it is the one that actually matters: **status is
not a writable field.** No tool sets CANONICAL. `knowledge.promote` records an
adversarial retest, and the projection ignores a retest whose actor authored
the note. A run cannot promote its own note because promotion is a conclusion
the fold reaches, not a value anyone can assign.

Run identity is bound out of band by `bind_session`, never taken from tool
arguments. A model that could name its own `run_id` could attest as any run it
liked, and the disjointness rule would mean nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from forge.core.enums import RiskClass, SideEffect
from forge.errors import ForgeError
from forge.ids import new_id
from forge.knowledge import events as kev
from forge.knowledge.anchors import TreeSnapshot, anchor_for
from forge.knowledge.models import (
    MAX_BODY_CHARS,
    Anchor,
    Attestation,
    Note,
    NoteId,
    NoteKind,
    RejectReason,
    RunId,
    Verdict,
)
from forge.knowledge.outcomes import resolve_terminal
from forge.knowledge.policy import PromotionPolicy
from forge.knowledge.projection import project
from forge.knowledge.store import KnowledgeLog
from forge.tools.registry import ToolOutcome, ToolRegistry

__all__ = [
    "AGENT_TOOLS",
    "LIBRARIAN_TOOLS",
    "KnowledgeRejected",
    "KnowledgeSession",
    "bind_session",
    "current_session",
]


class KnowledgeRejected(ForgeError):
    """A write refused at the boundary, before it can reach the log.

    Rejection beats filtering: evidence the projection would have to ignore
    should never have been recorded as evidence.
    """

    def __init__(self, reason: RejectReason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason: RejectReason = reason
        self.detail = detail


@dataclass
class KnowledgeSession:
    """Per-run binding: who is writing, and what they have already read."""

    log: KnowledgeLog
    run_id: RunId
    reader_lineage: tuple[NoteId, ...] = ()
    tree: TreeSnapshot = field(default_factory=dict)
    policy: PromotionPolicy = field(default_factory=PromotionPolicy)
    is_librarian: bool = False


_SESSION: KnowledgeSession | None = None


def bind_session(session: KnowledgeSession | None) -> None:
    """Install the session the knowledge tools act under."""
    global _SESSION
    _SESSION = session


def current_session() -> KnowledgeSession:
    if _SESSION is None:
        raise KnowledgeRejected("UNKNOWN_NOTE", "no knowledge session is bound")
    return _SESSION


AGENT_TOOLS = ToolRegistry()
LIBRARIAN_TOOLS = ToolRegistry()


# -- knowledge.write --------------------------------------------------------


class WriteArgs(BaseModel):
    kind: str = Field(description="LANDMARK | RECIPE | HAZARD | CONSTRAINT | MODEL_FACT")
    scope: str = Field(default="repo", description="'repo', a glob, or a symbol name.")
    body: str = Field(description=f"The insight, at most {MAX_BODY_CHARS} characters.")
    path: str | None = Field(default=None, description="File this note is about.")
    region: str | None = Field(
        default=None, description="Symbol name, or 'start-end' line span."
    )
    derived_from: list[str] = Field(default_factory=list)


def _parse_region(region: str) -> tuple[int, int] | str:
    if "-" in region:
        head, _, tail = region.partition("-")
        if head.strip().isdigit() and tail.strip().isdigit():
            return (int(head), int(tail))
    return region


async def _quarantine_note(**kwargs: object) -> None:
    """Compensator for `knowledge.write`: take the note out of circulation."""
    session = current_session()
    note_id = str(kwargs.get("note_id", ""))
    if note_id:
        await session.log.append(
            kev.make_note_quarantined(
                note_id=note_id, actor=session.run_id, reason="compensated write"
            )
        )


@AGENT_TOOLS.tool(
    name="knowledge.write",
    description="Record an insight about this repository so other runs can find it.",
    args=WriteArgs,
    side_effect=SideEffect.REVERSIBLE_WRITE,
    capability="KNOWLEDGE_WRITE",
    risk=RiskClass.LOW,
    compensate=_quarantine_note,
)
async def knowledge_write(
    kind: str,
    body: str,
    scope: str = "repo",
    path: str | None = None,
    region: str | None = None,
    derived_from: list[str] | None = None,
) -> ToolOutcome:
    session = current_session()

    text = body.strip()
    if not text:
        raise KnowledgeRejected("BODY_EMPTY", "a note with no body asserts nothing")
    if len(text) > session.policy.max_body_chars:
        raise KnowledgeRejected(
            "BODY_TOO_LONG",
            f"{len(text)} chars exceeds the {session.policy.max_body_chars} limit; "
            "split it into separate notes so each can be corroborated on its own",
        )

    anchors: tuple[Anchor, ...] = ()
    if path and region:
        content = session.tree.get(path)
        if content is not None:
            built = anchor_for(path, content, _parse_region(region))
            if built is not None:
                anchors = (built,)

    note = Note(
        id=new_id("note"),
        kind=kind,  # type: ignore[arg-type]  # constrained by the schema enum
        scope=scope,
        body=text,
        anchors=anchors,
        author_run_id=session.run_id,
        derived_from=tuple(derived_from or ()),
    )
    result = await session.log.append(kev.make_note_proposed(note))

    # A dedupe hit means this run already wrote this exact note. Report the
    # note that actually exists, not the id we just minted and discarded.
    stored_id = str(result.event.payload.get("note_id", note.id))
    return ToolOutcome(
        ok=True,
        output=f"note {stored_id} recorded as CLAIM",
        evidence={
            "applied": True,
            "note_id": stored_id,
            "deduplicated": result.deduplicated,
            "anchored": bool(anchors),
        },
    )


# -- knowledge.attest -------------------------------------------------------


class AttestArgs(BaseModel):
    note_id: str = Field(description="The note this outcome bears on.")
    event_seq: int = Field(description="Sequence number of this run's terminal event.")
    verdict: str = Field(description="SUPPORT or REFUTE")


async def _retract_attestation(**kwargs: object) -> None:
    """Compensator for `knowledge.attest`.

    Nothing is deleted - the retraction is itself an event, and the projection
    stops counting the attestation. An append-only log does not forget; it
    records that you changed your mind.
    """
    session = current_session()
    note_id = str(kwargs.get("note_id", ""))
    if note_id:
        await session.log.append(
            kev.make_attestation_retracted(
                note_id=note_id,
                run_id=session.run_id,
                event_seq=int(str(kwargs.get("event_seq", 0))),
                verdict="SUPPORT" if kwargs.get("verdict") == "SUPPORT" else "REFUTE",
                actor=session.run_id,
                reason="compensated attestation",
            )
        )


@AGENT_TOOLS.tool(
    name="knowledge.attest",
    description=(
        "Cite this run's outcome as evidence for or against a note. "
        "Only a terminal outcome counts, and only another run's note."
    ),
    args=AttestArgs,
    side_effect=SideEffect.REVERSIBLE_WRITE,
    capability="KNOWLEDGE_ATTEST",
    risk=RiskClass.LOW,
    compensate=_retract_attestation,
)
async def knowledge_attest(note_id: str, event_seq: int, verdict: str) -> ToolOutcome:
    session = current_session()
    if verdict not in ("SUPPORT", "REFUTE"):
        raise KnowledgeRejected("NON_PASSING_SUPPORT", f"unknown verdict {verdict!r}")
    typed: Verdict = "SUPPORT" if verdict == "SUPPORT" else "REFUTE"

    run_events = await session.log.read_run(session.run_id)
    resolved = resolve_terminal(run_events, run_id=session.run_id, event_seq=event_seq)
    if resolved is None:
        raise KnowledgeRejected(
            "UNRESOLVED_EVENT_SEQ",
            f"seq {event_seq} is not a terminal event of run {session.run_id}; "
            "an attestation must point at an outcome anyone can re-check",
        )
    event_type, outcome = resolved

    # A run that failed cannot be evidence *for* anything. Refutations may cite
    # either outcome: failing because of a note is what a refutation is made of.
    if typed == "SUPPORT" and not outcome.is_passing:
        raise KnowledgeRejected(
            "NON_PASSING_SUPPORT",
            f"{event_type.value} is not a passing outcome; a failed run cannot support a note",
        )

    attestation = Attestation(
        note_id=note_id,
        run_id=session.run_id,
        event_seq=event_seq,
        verdict=typed,
        outcome=outcome,
        outcome_event=event_type.value,
        reader_lineage=session.reader_lineage,
    )
    result = await session.log.append(kev.make_attestation_recorded(attestation))
    return ToolOutcome(
        ok=True,
        output=f"{typed} recorded for {note_id}",
        evidence={
            "applied": True,
            "note_id": note_id,
            "deduplicated": result.deduplicated,
            "outcome": str(outcome),
        },
    )


# -- knowledge.read ---------------------------------------------------------


class ReadArgs(BaseModel):
    scope: str | None = Field(default=None, description="Filter by note scope.")
    min_status: str = Field(
        default="CLAIM", description="CLAIM | CORROBORATED | CANONICAL"
    )


_RANK = {"REFUTED": -1, "QUARANTINED": -1, "STALE": 0, "CLAIM": 1, "CORROBORATED": 2, "CANONICAL": 3}


@AGENT_TOOLS.tool(
    name="knowledge.read",
    description="Retrieve what other runs have established about this repository.",
    args=ReadArgs,
    side_effect=SideEffect.READ,
    capability="KNOWLEDGE_READ",
)
async def knowledge_read(scope: str | None = None, min_status: str = "CLAIM") -> ToolOutcome:
    session = current_session()
    events = await session.log.read_knowledge()
    state = project(events, session.tree, session.policy)

    floor = _RANK.get(min_status, 1)
    hits = [
        s
        for s in state.notes.values()
        if _RANK.get(s.status, 0) >= floor and (scope is None or s.note.scope == scope)
    ]
    hits.sort(key=lambda s: (-_RANK.get(s.status, 0), s.note.id))

    lines = [f"[{s.status}] {s.note.id} ({s.note.kind}) {s.note.body}" for s in hits]
    return ToolOutcome(
        ok=True,
        output="\n".join(lines) if lines else "no notes at or above that status",
        evidence={"count": len(hits), "note_ids": [s.note.id for s in hits]},
    )


# -- librarian surface ------------------------------------------------------


class PromoteArgs(BaseModel):
    note_id: str
    passed: bool = Field(description="Did the adversarial retest pass?")
    detail: str = Field(default="", description="What was retested, and how.")


async def _quarantine_for_promote(**kwargs: object) -> None:
    session = current_session()
    note_id = str(kwargs.get("note_id", ""))
    if note_id:
        await session.log.append(
            kev.make_note_quarantined(
                note_id=note_id, actor=session.run_id, reason="compensated retest"
            )
        )


@LIBRARIAN_TOOLS.tool(
    name="knowledge.promote",
    description=(
        "Record the result of an adversarial retest. This does not set a status - "
        "status is derived - it supplies the one CANONICAL input that is not an attestation."
    ),
    args=PromoteArgs,
    side_effect=SideEffect.REVERSIBLE_WRITE,
    capability="KNOWLEDGE_PROMOTE",
    risk=RiskClass.MEDIUM,
    compensate=_quarantine_for_promote,
)
async def knowledge_promote(note_id: str, passed: bool, detail: str = "") -> ToolOutcome:
    session = current_session()
    await session.log.append(
        kev.make_adversarial_retest(
            note_id=note_id, passed=passed, actor=session.run_id, detail=detail
        )
    )
    return ToolOutcome(
        ok=True,
        output=f"adversarial retest recorded for {note_id}: {'passed' if passed else 'failed'}",
        evidence={"applied": True, "note_id": note_id, "passed": passed},
    )


class ReanchorArgs(BaseModel):
    note_id: str
    path: str
    region: str


@LIBRARIAN_TOOLS.tool(
    name="knowledge.reanchor",
    description="Re-pin a stale note to the region as it now stands.",
    args=ReanchorArgs,
    side_effect=SideEffect.REVERSIBLE_WRITE,
    capability="KNOWLEDGE_PROMOTE",
    compensate=_quarantine_for_promote,
)
async def knowledge_reanchor(note_id: str, path: str, region: str) -> ToolOutcome:
    session = current_session()
    content = session.tree.get(path)
    if content is None:
        raise KnowledgeRejected("UNKNOWN_NOTE", f"{path} is not in the tree snapshot")
    anchor = anchor_for(path, content, _parse_region(region))
    if anchor is None:
        raise KnowledgeRejected("UNKNOWN_NOTE", f"cannot locate {region!r} in {path}")
    await session.log.append(
        kev.make_note_reanchored(
            note_id=note_id,
            anchors=(anchor,),
            observed_at=anchor.content_hash[:12],
            actor=session.run_id,
        )
    )
    return ToolOutcome(
        ok=True,
        output=f"{note_id} re-anchored to {path}:{region}",
        evidence={"applied": True, "note_id": note_id},
    )


class CurateArgs(BaseModel):
    note_id: str
    reason: str = ""


@LIBRARIAN_TOOLS.tool(
    name="knowledge.quarantine",
    description="Take a note out of circulation pending review.",
    args=CurateArgs,
    side_effect=SideEffect.REVERSIBLE_WRITE,
    capability="KNOWLEDGE_PROMOTE",
    compensate=_quarantine_for_promote,
)
async def knowledge_quarantine(note_id: str, reason: str = "") -> ToolOutcome:
    session = current_session()
    await session.log.append(
        kev.make_note_quarantined(note_id=note_id, actor=session.run_id, reason=reason)
    )
    return ToolOutcome(
        ok=True, output=f"{note_id} quarantined", evidence={"applied": True, "note_id": note_id}
    )


def note_kinds() -> tuple[NoteKind, ...]:
    """The kinds a note may take. Exposed for schema generation and tests."""
    return ("LANDMARK", "RECIPE", "HAZARD", "CONSTRAINT", "MODEL_FACT")
