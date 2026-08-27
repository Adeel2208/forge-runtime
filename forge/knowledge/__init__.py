"""Shared knowledge between runs, where nothing is true because it was asserted.

A note is only promotable when a verifiable outcome in the event log supports
it, and only when the runs supplying that support did not get it from the note
itself. Agreement between correlated runs is not evidence; it is an echo.

    from forge.knowledge import PromotionPolicy, project

    state = project(events, tree_snapshot, PromotionPolicy())
    state.status("note_1f4c")        # -> "CORROBORATED"

Status is never stored. See docs/adr/0007-knowledge-status-is-a-projection.md.
"""

from __future__ import annotations

from forge.knowledge.anchors import anchor_for, is_stale, stale_anchors
from forge.knowledge.lineage import LineageGraph
from forge.knowledge.models import (
    Anchor,
    Attestation,
    KnowledgeState,
    Note,
    NoteId,
    NoteKind,
    NoteState,
    NoteStatus,
    RunId,
    Verdict,
)
from forge.knowledge.outcomes import OutcomeClass, register_terminal, resolve_terminal
from forge.knowledge.policy import PromotionPolicy
from forge.knowledge.projection import project
from forge.knowledge.store import InMemoryKnowledgeLog, KnowledgeLog, SQLiteKnowledgeLog

__all__ = [
    "Anchor",
    "Attestation",
    "InMemoryKnowledgeLog",
    "KnowledgeLog",
    "KnowledgeState",
    "LineageGraph",
    "Note",
    "NoteId",
    "NoteKind",
    "NoteState",
    "NoteStatus",
    "OutcomeClass",
    "PromotionPolicy",
    "RunId",
    "SQLiteKnowledgeLog",
    "Verdict",
    "anchor_for",
    "is_stale",
    "project",
    "register_terminal",
    "resolve_terminal",
    "stale_anchors",
]
