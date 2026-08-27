"""The promotion predicate, its precedence order, and the full status arc."""

from __future__ import annotations

import pytest

from forge.knowledge.anchors import anchor_for
from forge.knowledge.models import Anchor
from forge.knowledge.policy import PromotionPolicy
from forge.knowledge.projection import project
from tests.knowledge.conftest import SOURCE_V1, SOURCE_V2, Log

POLICY = PromotionPolicy()


# -- the arc ----------------------------------------------------------------


def test_the_full_lifecycle(log: Log) -> None:
    """CLAIM -> CORROBORATED -> CANONICAL -> STALE, one note, one log."""
    anchor = anchor_for("forge/retry.py", SOURCE_V1, "retry")
    assert anchor is not None
    tree = {"forge/retry.py": SOURCE_V1}

    note = log.note(author="run_author", anchors=(anchor,))
    assert project(log.events, tree, POLICY).status(note.id) == "CLAIM"

    log.corroborate(note, runs=2)
    assert project(log.events, tree, POLICY).status(note.id) == "CORROBORATED"

    log.corroborate(note, runs=2, start=3)
    assert project(log.events, tree, POLICY).status(note.id) == "CORROBORATED", (
        "four supports is not enough on its own: the adversarial retest is the "
        "difference between agreement and verification"
    )

    log.retest(note, passed=True)
    assert project(log.events, tree, POLICY).status(note.id) == "CANONICAL"

    changed = {"forge/retry.py": SOURCE_V2}
    assert project(log.events, changed, POLICY).status(note.id) == "STALE"


# -- precedence -------------------------------------------------------------


def test_quarantine_outranks_staleness(log: Log) -> None:
    anchor = anchor_for("forge/retry.py", SOURCE_V1, "retry")
    assert anchor is not None
    note = log.note(author="run_author", anchors=(anchor,))
    log.quarantine(note, reason="under review")

    state = project(log.events, {"forge/retry.py": SOURCE_V2}, POLICY).notes[note.id]
    assert state.status == "QUARANTINED"
    assert state.stale_anchors, "it is genuinely stale too; the human act just wins"


def test_staleness_outranks_corroboration(log: Log) -> None:
    anchor = anchor_for("forge/retry.py", SOURCE_V1, "retry")
    assert anchor is not None
    note = log.note(author="run_author", anchors=(anchor,))
    log.corroborate(note, runs=6)
    log.retest(note, passed=True)

    assert project(log.events, {"forge/retry.py": SOURCE_V2}, POLICY).status(note.id) == "STALE"


def test_refutation_outranks_support(log: Log) -> None:
    note = log.note(author="run_author")
    log.corroborate(note, runs=2)
    for i in range(3):
        log.attest(note, run_id=f"run_ref_{i}", verdict="REFUTE", passing=False)

    state = project(log.events, {}, POLICY).notes[note.id]
    assert state.status == "REFUTED"
    assert state.independent_refute == 3
    assert state.independent_support == 2


def test_a_single_refutation_blocks_canonical(log: Log) -> None:
    """CANONICAL requires zero refutation, not merely a majority."""
    note = log.note(author="run_author")
    log.corroborate(note, runs=6)
    log.retest(note, passed=True)
    log.attest(note, run_id="run_dissent", verdict="REFUTE", passing=False)

    state = project(log.events, {}, POLICY).notes[note.id]
    assert state.status == "CORROBORATED"
    assert state.independent_refute == 1


# -- quarantine and release -------------------------------------------------


def test_release_returns_a_note_to_circulation(log: Log) -> None:
    note = log.note(author="run_author")
    log.corroborate(note, runs=2)
    log.quarantine(note)
    assert project(log.events, {}, POLICY).status(note.id) == "QUARANTINED"

    log.release(note)
    assert project(log.events, {}, POLICY).status(note.id) == "CORROBORATED"


def test_requarantine_after_release_sticks(log: Log) -> None:
    note = log.note(author="run_author")
    log.quarantine(note)
    log.release(note)
    log.quarantine(note, reason="again")
    assert project(log.events, {}, POLICY).status(note.id) == "QUARANTINED"


# -- retests ----------------------------------------------------------------


def test_a_failed_retest_does_not_canonise(log: Log) -> None:
    note = log.note(author="run_author")
    log.corroborate(note, runs=4)
    log.retest(note, passed=False)
    assert project(log.events, {}, POLICY).status(note.id) == "CORROBORATED"


def test_a_later_retest_supersedes_an_earlier_one(log: Log) -> None:
    note = log.note(author="run_author")
    log.corroborate(note, runs=4)
    log.retest(note, passed=True)
    assert project(log.events, {}, POLICY).status(note.id) == "CANONICAL"

    log.retest(note, passed=False, actor="run_librarian_2")
    assert project(log.events, {}, POLICY).status(note.id) == "CORROBORATED"


def test_retest_can_be_waived_by_policy(log: Log) -> None:
    note = log.note(author="run_author")
    log.corroborate(note, runs=4)
    policy = PromotionPolicy(require_adversarial_retest=False)
    assert project(log.events, {}, policy).status(note.id) == "CANONICAL"


# -- unanchored notes -------------------------------------------------------


def test_an_unanchored_note_never_goes_stale(log: Log) -> None:
    """Nothing in the tree can contradict a note that points at nothing.

    A real limitation, named in ADR-0007: unanchored notes rest entirely on
    corroboration and refutation.
    """
    note = log.note(author="run_author", scope="repo", anchors=())
    log.corroborate(note, runs=4)
    log.retest(note, passed=True)
    assert project(log.events, {"anything.py": "totally different"}, POLICY).status(
        note.id
    ) == "CANONICAL"


# -- retraction -------------------------------------------------------------


def test_retracting_an_attestation_removes_it_from_the_count(log: Log) -> None:
    from forge.knowledge import events as kev

    note = log.note(author="run_author")
    runs = log.corroborate(note, runs=3)
    assert project(log.events, {}, POLICY).notes[note.id].independent_support == 3

    victim = next(
        a
        for a in log.events
        if a.type.value == "ATTESTATION_RECORDED" and a.payload["run_id"] == runs[0]
    )
    log.add(
        kev.make_attestation_retracted(
            note_id=note.id,
            run_id=runs[0],
            event_seq=int(victim.payload["event_seq"]),
            verdict="SUPPORT",
            actor=runs[0],
            reason="mistaken",
        )
    )

    state = project(log.events, {}, POLICY).notes[note.id]
    assert state.independent_support == 2
    assert len(state.discounted_for("RETRACTED")) == 1
    assert state.attestation_count == 3, "nothing was deleted; it stopped counting"


# -- robustness -------------------------------------------------------------


def test_an_attestation_for_an_unknown_note_is_recorded_as_rejected(log: Log) -> None:
    log.attest("note_does_not_exist", run_id="run_b")
    state = project(log.events, {}, POLICY)
    assert state.notes == {}
    assert any(reason == "UNKNOWN_NOTE" for reason, _ in state.rejected)


def test_unknown_event_types_only_advance_the_watermark(log: Log) -> None:
    """Forward-compatibility: an older reader must survive a newer writer."""
    note = log.note(author="run_author")
    tail = log.terminal("run_zzz")
    state = project(log.events, {}, POLICY)
    assert state.last_seq == tail
    assert state.status(note.id) == "CLAIM"


def test_empty_log_projects_to_nothing() -> None:
    state = project([], {}, POLICY)
    assert state.notes == {}
    assert state.last_seq == 0


def test_policy_rejects_incoherent_thresholds() -> None:
    with pytest.raises(ValueError, match="canonical_support"):
        PromotionPolicy(corroborated_support=4, canonical_support=2)
    with pytest.raises(ValueError, match="corroborated_support"):
        PromotionPolicy(corroborated_support=0)


def test_absorbed_notes_do_not_appear_as_separate_entries(log: Log) -> None:
    body = "identical wording"
    a = log.note(author="run_1", body=body)
    b = log.note(author="run_2", body=body)
    state = project(log.events, {}, POLICY)
    present = [n for n in (a.id, b.id) if n in state.notes]
    assert len(present) == 1
    assert state.notes[present[0]].absorbed == tuple(
        sorted({a.id, b.id} - {present[0]})
    )


def test_same_words_about_a_different_region_are_different_notes(log: Log) -> None:
    """Anchors participate in identity: the same sentence about two functions
    is two claims, and collapsing them would merge unrelated evidence."""
    one = Anchor(path="a.py", region="f", content_hash="h1")
    two = Anchor(path="b.py", region="g", content_hash="h2")
    a = log.note(author="run_1", body="not thread safe", anchors=(one,))
    b = log.note(author="run_2", body="not thread safe", anchors=(two,))

    state = project(log.events, {}, POLICY)
    assert a.id in state.notes
    assert b.id in state.notes
