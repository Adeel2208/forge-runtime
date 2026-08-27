"""The tool surfaces: write-time rejection, and the barriers around promotion."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from forge.core.enums import SideEffect
from forge.knowledge.policy import PromotionPolicy
from forge.knowledge.projection import project
from forge.knowledge.store import InMemoryKnowledgeLog
from forge.knowledge.tools import (
    AGENT_TOOLS,
    LIBRARIAN_TOOLS,
    KnowledgeRejected,
    KnowledgeSession,
    bind_session,
    knowledge_attest,
    knowledge_promote,
    knowledge_read,
    knowledge_write,
)
from tests.knowledge.conftest import SOURCE_V1, Log


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def session() -> Iterator[KnowledgeSession]:
    sess = KnowledgeSession(
        log=InMemoryKnowledgeLog(),
        run_id="run_writer",
        tree={"forge/retry.py": SOURCE_V1},
    )
    bind_session(sess)
    try:
        yield sess
    finally:
        bind_session(None)


# -- write-time rejection ---------------------------------------------------


def test_an_overlong_body_is_refused(session: KnowledgeSession) -> None:
    with pytest.raises(KnowledgeRejected) as caught:
        run(knowledge_write(kind="LANDMARK", body="x" * 501))
    assert caught.value.reason == "BODY_TOO_LONG"
    assert "split it into separate notes" in caught.value.detail


def test_a_body_at_the_limit_is_accepted(session: KnowledgeSession) -> None:
    outcome = run(knowledge_write(kind="LANDMARK", body="x" * 500))
    assert outcome.ok


def test_an_empty_body_is_refused(session: KnowledgeSession) -> None:
    with pytest.raises(KnowledgeRejected) as caught:
        run(knowledge_write(kind="LANDMARK", body="   "))
    assert caught.value.reason == "BODY_EMPTY"


def test_writing_without_a_session_is_refused() -> None:
    bind_session(None)
    with pytest.raises(KnowledgeRejected):
        run(knowledge_write(kind="LANDMARK", body="orphaned"))


# -- authorship comes from the runtime, never the model ---------------------


def test_the_author_is_the_bound_run_not_an_argument(session: KnowledgeSession) -> None:
    """`knowledge.write` takes no run_id. A model that could name its own run
    could attest as any run it liked, and disjointness would mean nothing."""
    schema = AGENT_TOOLS.get("knowledge.write").json_schema()
    properties = schema["parameters"]["properties"]
    assert "run_id" not in properties
    assert "author_run_id" not in properties

    run(knowledge_write(kind="LANDMARK", body="an insight"))
    events = run(session.log.read_knowledge())
    assert events[0].payload["author_run_id"] == "run_writer"


def test_attest_takes_no_run_id_either() -> None:
    properties = AGENT_TOOLS.get("knowledge.attest").json_schema()["parameters"]["properties"]
    assert "run_id" not in properties


# -- anchoring through the tool --------------------------------------------


def test_a_write_with_a_symbol_anchors_it(session: KnowledgeSession) -> None:
    outcome = run(
        knowledge_write(
            kind="HAZARD",
            body="retry swallows OSError only",
            path="forge/retry.py",
            region="retry",
        )
    )
    assert outcome.evidence["anchored"] is True


def test_a_write_naming_an_absent_file_still_records_the_note(
    session: KnowledgeSession,
) -> None:
    """Losing the anchor is better than losing the insight."""
    outcome = run(
        knowledge_write(kind="LANDMARK", body="a fact", path="nope.py", region="thing")
    )
    assert outcome.ok
    assert outcome.evidence["anchored"] is False


def test_a_repeated_write_from_one_run_dedupes(session: KnowledgeSession) -> None:
    first = run(knowledge_write(kind="LANDMARK", body="the same insight"))
    second = run(knowledge_write(kind="LANDMARK", body="the same insight"))
    assert second.evidence["deduplicated"] is True
    assert first.evidence["note_id"] == second.evidence["note_id"]


# -- registry separation ----------------------------------------------------


def test_the_two_registries_are_disjoint() -> None:
    assert set(AGENT_TOOLS.names()).isdisjoint(set(LIBRARIAN_TOOLS.names()))


def test_the_agent_surface_is_exactly_write_attest_read() -> None:
    assert set(AGENT_TOOLS.names()) == {
        "knowledge.write",
        "knowledge.attest",
        "knowledge.read",
    }


def test_promotion_tools_require_a_distinct_capability() -> None:
    """A worker granted KNOWLEDGE_WRITE gains nothing toward promotion."""
    agent_caps = {AGENT_TOOLS.get(n).capability for n in AGENT_TOOLS.names()}
    librarian_caps = {LIBRARIAN_TOOLS.get(n).capability for n in LIBRARIAN_TOOLS.names()}
    assert agent_caps.isdisjoint(librarian_caps)
    assert librarian_caps == {"KNOWLEDGE_PROMOTE"}


def test_writes_declare_compensators() -> None:
    """A REVERSIBLE_WRITE without a compensator cannot be registered at all."""
    for registry in (AGENT_TOOLS, LIBRARIAN_TOOLS):
        for name in registry.names():
            spec = registry.get(name)
            if spec.side_effect is SideEffect.REVERSIBLE_WRITE:
                assert spec.compensate is not None, f"{name} has no compensator"


# -- promotion is not a writable status -------------------------------------


def test_promote_records_a_retest_rather_than_setting_a_status(
    session: KnowledgeSession,
) -> None:
    """There is no code path that assigns CANONICAL. That is the guarantee."""
    log = Log()
    note = log.note(author="run_author")
    log.corroborate(note, runs=4)
    session.log.extend(log.events)
    session.run_id = "run_librarian"

    run(knowledge_promote(note_id=note.id, passed=True, detail="re-ran the case set"))

    events = run(session.log.read_knowledge())
    assert not any("status" in e.payload for e in events)

    state = project(events, {}, PromotionPolicy())
    assert state.status(note.id) == "CANONICAL", "derived, not assigned"


def test_a_librarian_cannot_canonise_its_own_note(session: KnowledgeSession) -> None:
    """Holding the capability is not enough: the projection checks authorship."""
    run(knowledge_write(kind="LANDMARK", body="my own insight"))
    events = run(session.log.read_knowledge())
    note_id = str(events[0].payload["note_id"])

    log = Log()
    for i in range(4):
        session.log.extend([log.terminal(f"run_x_{i}") and log.events[-1]])

    run(knowledge_promote(note_id=note_id, passed=True))

    state = project(run(session.log.read_knowledge()), {}, PromotionPolicy())
    assert state.notes[note_id].retest_passed is False


# -- compensation -----------------------------------------------------------


def test_the_attest_compensator_retracts_rather_than_deletes(
    session: KnowledgeSession,
) -> None:
    log = Log()
    note = log.note(author="run_author")
    session.log.extend(log.events)
    session.run_id = "run_attester"

    seq = run(session.log.append(_terminal("run_attester"))).event.seq
    run(knowledge_attest(note.id, seq, "SUPPORT"))
    before = project(run(session.log.read_knowledge()), {}, PromotionPolicy())
    assert before.notes[note.id].independent_support == 1

    spec = AGENT_TOOLS.get("knowledge.attest")
    assert spec.compensate is not None
    run(spec.compensate(note_id=note.id, event_seq=seq, verdict="SUPPORT"))

    after = project(run(session.log.read_knowledge()), {}, PromotionPolicy())
    assert after.notes[note.id].independent_support == 0
    assert len(after.notes[note.id].discounted_for("RETRACTED")) == 1
    assert after.notes[note.id].attestation_count == 1, "the record remains"


def _terminal(run_id: str) -> Any:
    from forge.core.enums import EventType
    from forge.core.events import NewEvent

    return NewEvent(type=EventType.RUN_COMPLETED, run_id=run_id, payload={})


# -- read -------------------------------------------------------------------


def test_read_filters_by_minimum_status(session: KnowledgeSession) -> None:
    log = Log()
    weak = log.note(author="run_a", body="unproven claim")
    strong = log.note(author="run_b", body="well established")
    log.corroborate(strong, runs=3)
    session.log.extend(log.events)

    everything = run(knowledge_read())
    assert everything.evidence["count"] == 2

    only_solid = run(knowledge_read(min_status="CORROBORATED"))
    assert only_solid.evidence["note_ids"] == [strong.id]
    assert weak.id not in only_solid.evidence["note_ids"]


def test_read_reports_status_alongside_body(session: KnowledgeSession) -> None:
    log = Log()
    note = log.note(author="run_a", body="the thing to know")
    log.corroborate(note, runs=2)
    session.log.extend(log.events)

    outcome = run(knowledge_read())
    assert "[CORROBORATED]" in outcome.output
    assert "the thing to know" in outcome.output
