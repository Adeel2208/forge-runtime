"""Promotion thresholds and the pure predicates that apply them.

Everything here is data plus total functions. `PromotionPolicy` is frozen and
carries no I/O, which is what makes the retroactivity property hold: the same
event history under two policies yields two coherent projections, and neither
requires replaying anything but the log.
"""

from __future__ import annotations

from dataclasses import dataclass

from forge.knowledge.lineage import LineageGraph
from forge.knowledge.models import (
    MAX_BODY_CHARS,
    Attestation,
    DiscountReason,
    Note,
    RunId,
)

__all__ = ["PromotionPolicy", "discount_reason"]


@dataclass(frozen=True)
class PromotionPolicy:
    """Thresholds for status. Frozen so a projection cannot mutate its own rules."""

    corroborated_support: int = 2
    canonical_support: int = 4
    require_adversarial_retest: bool = True
    max_body_chars: int = MAX_BODY_CHARS

    count_self_authored: bool = False
    """Off by default and should stay off. On, a run can promote its own note
    by attesting to it repeatedly - the failure this whole layer exists to
    prevent. Exposed only so a test can demonstrate the difference."""

    count_lineage_contaminated: bool = False
    """Off by default. On, reading a note and then succeeding counts as
    support, and the store converges on whatever was written first."""

    quarantine_dominates_staleness: bool = True
    """A note that is both quarantined and stale reads as QUARANTINED: a
    deliberate human act outranks a mechanical observation."""

    def __post_init__(self) -> None:
        if self.corroborated_support < 1:
            raise ValueError("corroborated_support must be at least 1")
        if self.canonical_support < self.corroborated_support:
            raise ValueError(
                "canonical_support must be >= corroborated_support; otherwise a note "
                "could be CANONICAL without ever being CORROBORATED"
            )
        if self.max_body_chars < 1:
            raise ValueError("max_body_chars must be positive")


def discount_reason(
    attestation: Attestation,
    note: Note,
    graph: LineageGraph,
    *,
    authors: frozenset[RunId],
    counted_runs: frozenset[RunId],
    retracted: bool,
    policy: PromotionPolicy,
) -> DiscountReason | None:
    """Why this attestation does not count, or None when it does.

    Order matters only for the quality of the explanation, not the outcome:
    a self-authored attestation from a run that also read the note is reported
    as SELF_AUTHORED because that is the more fundamental objection.
    """
    if retracted:
        return "RETRACTED"

    # A SUPPORT attestation must cite a passing outcome. Refutations may cite
    # either: a run that failed *because* of a note is exactly the evidence a
    # refutation is made of.
    if attestation.verdict == "SUPPORT" and not attestation.outcome.is_passing:
        return "NON_PASSING_OUTCOME"

    # The author exclusion is NOT implied by lineage. An authoring run never
    # "read" its own note, so reader_lineage will not contain it, and without
    # this clause a single run can manufacture a quorum by attesting five
    # times to something it wrote itself.
    #
    # `authors` is a set, not `note.author_run_id`, because identical bodies
    # from different runs collapse into one note. Every run that wrote that
    # content authored the survivor, and excluding only the elected survivor's
    # author would leave the sockpuppet attack working: write the same
    # sentence from four runs, attest from each, and three of them would read
    # as independent corroboration of the fourth.
    if attestation.run_id in authors and not policy.count_self_authored:
        return "SELF_AUTHORED"

    if not policy.count_lineage_contaminated and graph.contaminated(
        note.id, attestation.reader_lineage
    ):
        return "LINEAGE_CONTAMINATED"

    # Independence is counted per run, not per attestation. Five attestations
    # from one run are one run's worth of evidence.
    if attestation.run_id in counted_runs:
        return "DUPLICATE_RUN"

    return None
