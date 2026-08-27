"""Property tests for the knowledge fold.

The claims here are the ones ADR-0007 rests on. If any of them stops holding,
the promise that status is *derived* rather than *stored* has quietly become
false, and every promotion decision in the log is suspect.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Iterable

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from forge.core.events import Event
from forge.knowledge.models import KnowledgeState
from forge.knowledge.policy import PromotionPolicy
from forge.knowledge.projection import project
from tests.knowledge.conftest import Log

SETTINGS = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def _comparable(state: KnowledgeState) -> list[tuple[str, str, int, int, tuple[str, ...]]]:
    """A total, order-insensitive rendering of a projection, for equality."""
    return sorted(
        (
            note_id,
            s.status,
            s.independent_support,
            s.independent_refute,
            tuple(sorted(s.supporting_runs)),
        )
        for note_id, s in state.notes.items()
    )


def _build(seed: int, *, notes: int, attesters: int) -> Log:
    """A deterministic history with derivation, merges, quarantine and retests."""
    rng = random.Random(seed)
    log = Log()
    created = []
    for i in range(notes):
        parents = ()
        if created and rng.random() < 0.3:
            parents = (rng.choice(created).id,)
        created.append(
            log.note(
                author=f"run_author_{i}",
                body=f"insight number {i}",
                derived_from=parents,
            )
        )

    for note in created:
        for j in range(rng.randint(0, attesters)):
            lineage = ()
            if rng.random() < 0.25 and created:
                lineage = (rng.choice(created).id,)
            log.attest(
                note,
                run_id=f"run_att_{j}",
                verdict="SUPPORT" if rng.random() < 0.8 else "REFUTE",
                lineage=lineage,
            )

    if created and rng.random() < 0.5:
        log.retest(created[0], passed=True)
    if len(created) > 1 and rng.random() < 0.4:
        log.quarantine(created[1])
        if rng.random() < 0.5:
            log.release(created[1])
    return log


# -- 1. fold consistency ----------------------------------------------------


@SETTINGS
@given(seed=st.integers(0, 5000), split=st.integers(1, 6))
def test_folding_is_chunk_independent(seed: int, split: int) -> None:
    """Feeding the log through in chunks projects identically to one pass.

    Note the shape of this claim. `RunState` can be checkpointed and advanced,
    because a run's state depends only on its own prefix. Knowledge status
    cannot: a merge or a retraction arriving late changes the count for
    attestations folded long before it, so there is no prefix whose projection
    stays valid. The fold is therefore total over the whole log, and what we
    guarantee instead is that *how* the log is delivered cannot change the
    answer. ADR-0007 records why no knowledge checkpoint is offered.
    """
    log = _build(seed, notes=4, attesters=3)
    events = log.events
    size = max(1, len(events) // split)
    chunks: Iterable[list[Event]] = [
        events[i : i + size] for i in range(0, len(events), size)
    ]

    whole = project(events, {}, PromotionPolicy())
    chunked = project(itertools.chain.from_iterable(chunks), {}, PromotionPolicy())

    assert _comparable(whole) == _comparable(chunked)
    assert whole.last_seq == chunked.last_seq


# -- 2. order independence --------------------------------------------------


@SETTINGS
@given(seed=st.integers(0, 5000), shuffle_seed=st.integers(0, 5000))
def test_projection_is_independent_of_delivery_order(seed: int, shuffle_seed: int) -> None:
    """Shuffling the events changes nothing: every decision is keyed on `seq`.

    This is what lets two processes read the same log through different
    queries - one paginating, one streaming - and reach the same conclusion
    about every note.
    """
    log = _build(seed, notes=4, attesters=3)
    shuffled = list(log.events)
    random.Random(shuffle_seed).shuffle(shuffled)

    assert _comparable(project(log.events, {}, PromotionPolicy())) == _comparable(
        project(shuffled, {}, PromotionPolicy())
    )


# -- 3. checkpoint disposability -------------------------------------------


@SETTINGS
@given(seed=st.integers(0, 5000))
def test_recomputing_yields_an_identical_result(seed: int) -> None:
    """Projection is pure: no memo, no accumulated state, no drift.

    The knowledge equivalent of "drop every checkpoint and recompute". There
    is nothing cached to drop, which is the strongest form of the guarantee.
    """
    log = _build(seed, notes=4, attesters=3)
    policy = PromotionPolicy()

    first = project(log.events, {}, policy)
    _decoy = project(log.events[: len(log.events) // 2], {}, policy)
    second = project(log.events, {}, policy)

    assert _comparable(first) == _comparable(second)
    assert first.last_seq == second.last_seq


# -- 4. policy retroactivity ------------------------------------------------


@SETTINGS
@given(seed=st.integers(0, 5000))
def test_a_stricter_policy_never_promotes_more(seed: int) -> None:
    """The same history under two policies, neither needing a replay.

    Monotonicity is the useful form of the claim: raising the corroboration
    threshold can only demote. If a stricter policy ever promoted something a
    looser one did not, the thresholds would not mean what they say.
    """
    log = _build(seed, notes=4, attesters=4)
    rank = {"REFUTED": -1, "QUARANTINED": -1, "STALE": 0, "CLAIM": 1, "CORROBORATED": 2, "CANONICAL": 3}

    loose = project(log.events, {}, PromotionPolicy(corroborated_support=1, canonical_support=2))
    strict = project(log.events, {}, PromotionPolicy(corroborated_support=3, canonical_support=6))

    for note_id, strict_state in strict.notes.items():
        loose_state = loose.notes[note_id]
        assert rank[strict_state.status] <= rank[loose_state.status], (
            f"{note_id}: strict policy promoted to {strict_state.status} while the "
            f"looser one reached only {loose_state.status}"
        )


def test_policy_change_needs_no_migration() -> None:
    """Concretely: four supports is CORROBORATED at one threshold, CANONICAL at another."""
    log = Log()
    note = log.note(author="run_author")
    log.corroborate(note, runs=4)
    log.retest(note, passed=True)

    assert project(log.events, {}, PromotionPolicy(canonical_support=4)).status(note.id) == (
        "CANONICAL"
    )
    assert project(log.events, {}, PromotionPolicy(canonical_support=9)).status(note.id) == (
        "CORROBORATED"
    )
    # Same events, no rewrite, no migration: 1 note + 4x(terminal + attestation) + 1 retest.
    assert len(log.events) == 1 + 8 + 1
