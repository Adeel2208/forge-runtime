"""Negative fixtures: things the knowledge store must refuse to promote.

These are the knowledge layer's equivalent of the harness's non-compliant
targets. A green suite means nothing until you have shown it can go red, and
each fixture here asserts the *specific reason* promotion failed - not merely
that the status is not CANONICAL. A note that stays a CLAIM for the wrong
reason is a bug, and the weaker assertion would hide it.
"""

from __future__ import annotations

import pytest

from forge.knowledge.anchors import anchor_for
from forge.knowledge.policy import PromotionPolicy
from forge.knowledge.projection import project
from tests.knowledge.conftest import Log

POLICY = PromotionPolicy()


# 1 -------------------------------------------------------------------------


def test_a_run_cannot_corroborate_its_own_note(log: Log) -> None:
    """One run, five SUPPORT attestations for its own note. Stays a CLAIM.

    The author exclusion is not implied by lineage: an authoring run never
    *read* its own note, so `reader_lineage` is empty and only the explicit
    `run_id != author_run_id` clause catches this.
    """
    note = log.note(author="run_author")
    for _ in range(5):
        log.attest(note, run_id="run_author")

    state = project(log.events, {}, POLICY).notes[note.id]

    assert state.status == "CLAIM"
    assert state.independent_support == 0
    assert len(state.discounted_for("SELF_AUTHORED")) == 5
    assert state.attestation_count == 5, "the evidence is recorded, just not counted"


# 2 -------------------------------------------------------------------------


def test_reading_a_note_then_succeeding_is_not_support(log: Log) -> None:
    """Run B reads X, succeeds, attests. That is not evidence *for* X.

    It is evidence that X was not fatal. Those are different claims and only
    the second is licensed by the observation.
    """
    note = log.note(author="run_a")
    log.attest(note, run_id="run_b", lineage=(note.id,))

    state = project(log.events, {}, POLICY).notes[note.id]

    assert state.status == "CLAIM"
    assert state.independent_support == 0
    discounted = state.discounted_for("LINEAGE_CONTAMINATED")
    assert len(discounted) == 1
    assert discounted[0].run_id == "run_b"


# 3 -------------------------------------------------------------------------


def test_lineage_contamination_is_transitive(log: Log) -> None:
    """X derives from Y; a run that read only Y still cannot corroborate X."""
    parent = log.note(author="run_a", body="parent insight")
    child = log.note(author="run_b", body="child insight", derived_from=(parent.id,))

    log.attest(child, run_id="run_c", lineage=(parent.id,))

    state = project(log.events, {}, POLICY).notes[child.id]

    assert state.independent_support == 0
    assert len(state.discounted_for("LINEAGE_CONTAMINATED")) == 1


def test_merge_contaminates_in_both_directions(log: Log) -> None:
    """Reading an absorbed note contaminates you against the survivor."""
    survivor = log.note(author="run_a", body="survivor")
    absorbed = log.note(author="run_b", body="absorbed")
    log.merge(surviving=survivor.id, absorbed=absorbed.id)

    log.attest(survivor, run_id="run_c", lineage=(absorbed.id,))

    state = project(log.events, {}, POLICY).notes[survivor.id]
    assert state.independent_support == 0
    assert len(state.discounted_for("LINEAGE_CONTAMINATED")) == 1


# 4 -------------------------------------------------------------------------


def test_attestation_citing_a_non_terminal_event_is_rejected_at_write(log: Log) -> None:
    """An attestation must point at an outcome anyone can re-check.

    `STEP_STARTED` is a real event with a real sequence number, and it says
    nothing about how the run ended. Citing it is malformed evidence, so it is
    refused at the boundary rather than filtered in the projection.
    """
    import asyncio

    from forge.knowledge.store import InMemoryKnowledgeLog
    from forge.knowledge.tools import (
        KnowledgeRejected,
        KnowledgeSession,
        bind_session,
        knowledge_attest,
    )

    note = log.note(author="run_a")
    mid_run_seq = log.non_terminal("run_b")

    klog = InMemoryKnowledgeLog()
    klog.extend(log.events)

    bind_session(KnowledgeSession(log=klog, run_id="run_b"))
    try:
        with pytest.raises(KnowledgeRejected) as caught:
            asyncio.run(knowledge_attest(note.id, mid_run_seq, "SUPPORT"))
        assert caught.value.reason == "UNRESOLVED_EVENT_SEQ"
    finally:
        bind_session(None)


def test_attestation_citing_another_runs_terminal_is_rejected(log: Log) -> None:
    """You may only cite your own outcome, not borrow someone else's."""
    import asyncio

    from forge.knowledge.store import InMemoryKnowledgeLog
    from forge.knowledge.tools import (
        KnowledgeRejected,
        KnowledgeSession,
        bind_session,
        knowledge_attest,
    )

    note = log.note(author="run_a")
    someone_elses = log.terminal("run_c", passing=True)

    klog = InMemoryKnowledgeLog()
    klog.extend(log.events)

    bind_session(KnowledgeSession(log=klog, run_id="run_b"))
    try:
        with pytest.raises(KnowledgeRejected) as caught:
            asyncio.run(knowledge_attest(note.id, someone_elses, "SUPPORT"))
        assert caught.value.reason == "UNRESOLVED_EVENT_SEQ"
    finally:
        bind_session(None)


# 5 -------------------------------------------------------------------------


def test_support_citing_a_failing_outcome_is_rejected_at_write() -> None:
    """A run that failed cannot be evidence *for* anything."""
    import asyncio

    from forge.knowledge.store import InMemoryKnowledgeLog
    from forge.knowledge.tools import (
        KnowledgeRejected,
        KnowledgeSession,
        bind_session,
        knowledge_attest,
    )

    inner = Log()
    note = inner.note(author="run_a")
    failing_seq = inner.terminal("run_b", passing=False)

    klog = InMemoryKnowledgeLog()
    klog.extend(inner.events)

    bind_session(KnowledgeSession(log=klog, run_id="run_b"))
    try:
        with pytest.raises(KnowledgeRejected) as caught:
            asyncio.run(knowledge_attest(note.id, failing_seq, "SUPPORT"))
        assert caught.value.reason == "NON_PASSING_SUPPORT"
    finally:
        bind_session(None)


def test_refutation_may_cite_a_failing_outcome() -> None:
    """The mirror image: failing *because* of a note is what refutation is."""
    import asyncio

    from forge.knowledge.store import InMemoryKnowledgeLog
    from forge.knowledge.tools import KnowledgeSession, bind_session, knowledge_attest

    inner = Log()
    note = inner.note(author="run_a")
    failing_seq = inner.terminal("run_b", passing=False)

    klog = InMemoryKnowledgeLog()
    klog.extend(inner.events)

    bind_session(KnowledgeSession(log=klog, run_id="run_b"))
    try:
        outcome = asyncio.run(knowledge_attest(note.id, failing_seq, "REFUTE"))
        assert outcome.ok
    finally:
        bind_session(None)


# 6 -------------------------------------------------------------------------


def test_a_worker_cannot_reach_the_promote_tool() -> None:
    """`knowledge.promote` is not in the agent registry at all.

    Structural, not conditional: a worker's tool surface does not contain the
    symbol, so the refusal happens before any policy check runs.
    """
    from forge.knowledge.tools import AGENT_TOOLS, LIBRARIAN_TOOLS

    agent_names = set(AGENT_TOOLS.names())
    librarian_names = set(LIBRARIAN_TOOLS.names())

    assert "knowledge.promote" not in agent_names
    assert "knowledge.promote" in librarian_names
    assert agent_names.isdisjoint(librarian_names)


def test_a_run_cannot_canonise_a_note_it_authored(log: Log) -> None:
    """Even holding the librarian capability, a retest by the author is ignored.

    The second barrier. Capability gating stops the wrong *role*; this stops
    the right role acting on its own work.
    """
    note = log.note(author="run_author")
    log.corroborate(note, runs=4)
    log.retest(note, passed=True, actor="run_author")

    state = project(log.events, {}, POLICY).notes[note.id]

    assert state.independent_support == 4
    assert state.retest_passed is False
    assert state.status == "CORROBORATED"
    assert "awaiting adversarial retest" in state.reason


def test_a_librarian_retest_by_a_third_party_does_canonise(log: Log) -> None:
    """The control case: same evidence, disinterested retester."""
    note = log.note(author="run_author")
    log.corroborate(note, runs=4)
    log.retest(note, passed=True, actor="run_librarian")

    assert project(log.events, {}, POLICY).notes[note.id].status == "CANONICAL"


# 7 -------------------------------------------------------------------------


def test_a_stale_note_cannot_be_revived_by_fresh_attestations(
    log: Log, tree: dict[str, str], changed_tree: dict[str, str], retry_anchor: object
) -> None:
    """Canonical note, region changes, three fresh supports. Stays STALE.

    Staleness dominates corroboration: the note is about code that no longer
    exists in that form, so agreement about it is agreement about nothing.
    """
    from forge.knowledge.models import Anchor

    assert isinstance(retry_anchor, Anchor)
    note = log.note(author="run_author", anchors=(retry_anchor,))
    log.corroborate(note, runs=4)
    log.retest(note, passed=True)

    assert project(log.events, tree, POLICY).notes[note.id].status == "CANONICAL"

    # The anchored function changes.
    log.corroborate(note, runs=3, start=90)
    state = project(log.events, changed_tree, POLICY).notes[note.id]

    assert state.status == "STALE"
    assert state.independent_support == 7, "evidence still counted, status still stale"
    assert state.stale_anchors and state.stale_anchors[0].path == "forge/retry.py"


def test_reanchoring_is_what_revives_a_stale_note(
    log: Log, tree: dict[str, str], changed_tree: dict[str, str], retry_anchor: object
) -> None:
    """The resurrection path, and the only one."""
    from forge.knowledge.models import Anchor

    assert isinstance(retry_anchor, Anchor)
    note = log.note(author="run_author", anchors=(retry_anchor,))
    log.corroborate(note, runs=4)
    log.retest(note, passed=True)

    assert project(log.events, changed_tree, POLICY).notes[note.id].status == "STALE"

    fresh = anchor_for("forge/retry.py", changed_tree["forge/retry.py"], "retry")
    assert fresh is not None
    log.reanchor(note, (fresh,))

    assert project(log.events, changed_tree, POLICY).notes[note.id].status == "CANONICAL"


# 8 -------------------------------------------------------------------------


def test_identical_bodies_collapse_to_one_note(log: Log) -> None:
    """Four runs writing the same sentence is one note, not four.

    The store cannot do this - its unique index is `(run_id, key)` and each
    author produces a different key - so the collapse happens in the fold.
    """
    body = "the loop detector uses a tighter bound for mutating actions"
    notes = [log.note(author=f"run_{i}", body=body) for i in range(4)]

    state = project(log.events, {}, POLICY)

    surviving = [n for n in notes if n.id in state.notes]
    assert len(surviving) == 1, "four identical bodies must fold into one note"
    assert len(state.notes[surviving[0].id].absorbed) == 3


def test_manufactured_consensus_does_not_survive_the_collapse(log: Log) -> None:
    """The attack in full: write it four times, then attest from each author.

    After collapse every attestation is self-authored with respect to the
    surviving note, so the quorum evaporates.
    """
    body = "always pass check=True to subprocess.run"
    notes = [log.note(author=f"run_sock_{i}", body=body) for i in range(4)]
    for i, note in enumerate(notes):
        log.attest(note, run_id=f"run_sock_{i}")

    state = project(log.events, {}, POLICY)
    survivor = next(n.id for n in notes if n.id in state.notes)
    result = state.notes[survivor]

    assert result.status == "CLAIM"
    assert result.independent_support == 0
    assert len(result.discounted_for("SELF_AUTHORED")) == 4, (
        "every run that wrote this content authored the surviving note; "
        "excluding only the elected survivor's author would leave three "
        "sockpuppets reading as independent corroboration of the fourth"
    )
    assert len(result.absorbed) == 3


# 9 -------------------------------------------------------------------------


def test_a_retried_attestation_appends_once(log: Log) -> None:
    """Recording is the claim: one durable append against a unique index."""
    note = log.note(author="run_a")
    seq = log.terminal("run_b")

    before = len(log.events)
    for _ in range(4):
        log.attest(note, run_id="run_b", event_seq=seq)
    after = len(log.events)

    assert after - before == 1, "identical attestations must dedupe to one row"

    state = project(log.events, {}, POLICY).notes[note.id]
    assert state.independent_support == 1
    assert state.attestation_count == 1


def test_distinct_attestations_from_one_run_count_once(log: Log) -> None:
    """Different terminal events, same run. Still one run's worth of evidence."""
    note = log.note(author="run_a")
    for _ in range(4):
        log.attest(note, run_id="run_b")  # a fresh terminal event each time

    state = project(log.events, {}, POLICY).notes[note.id]

    assert state.attestation_count == 4, "four distinct rows landed"
    assert state.independent_support == 1, "but one run is one vote"
    assert len(state.discounted_for("DUPLICATE_RUN")) == 3
