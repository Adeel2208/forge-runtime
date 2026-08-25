"""Effect reconciliation (spec §5 RECONCILE, §12).

The question this phase answers is narrow and important: *did the thing we
authorized actually happen, and only that thing?* An agent runtime that skips
this cannot tell the difference between "the write succeeded", "the write
succeeded but the response was lost", and "a different write happened".

Verdicts drive what COMMIT does, so they are an enum, not a boolean.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from forge.core.contracts import Action, Effect
from forge.core.enums import RetryClass, SideEffect

__all__ = ["Reconciliation", "Verdict", "reconcile"]


class Verdict(StrEnum):
    MATCHED = "MATCHED"                # observed effect matches intent -> COMMIT
    REUSED = "REUSED"                  # idempotency hit -> COMMIT, no new effect
    BENIGN_FAILURE = "BENIGN_FAILURE"  # failed with no side effect -> COMMIT the failure
    RETRYABLE = "RETRYABLE"            # transient; same key, dispatch again
    NEEDS_COMPENSATION = "NEEDS_COMPENSATION"  # effect happened but is wrong
    MISMATCH = "MISMATCH"              # unexplained divergence -> fail closed


@dataclass(frozen=True)
class Reconciliation:
    verdict: Verdict
    reason: str
    compensate: bool = False


def reconcile(action: Action, effect: Effect) -> Reconciliation:
    """Compare an authorized intent against its observed effect."""
    if effect.idempotency_key != action.idempotency_key:
        # The evidence belongs to a different action. Never commit this.
        return Reconciliation(
            Verdict.MISMATCH,
            f"effect key {effect.idempotency_key!r} != action key "
            f"{action.idempotency_key!r}",
        )

    if effect.reused:
        return Reconciliation(Verdict.REUSED, "effect already recorded; dispatch suppressed")

    if effect.ok:
        return Reconciliation(Verdict.MATCHED, "observed effect matches authorized intent")

    # -- failure paths -----------------------------------------------------
    if effect.retry_class is RetryClass.TRANSIENT:
        # A read that failed transiently left nothing behind: retry freely.
        if action.side_effect is SideEffect.READ:
            return Reconciliation(Verdict.RETRYABLE, "transient failure on a read")

        # A write that failed transiently is the genuinely hard case: we do not
        # know whether the remote side applied it. The idempotency key makes a
        # retry safe, because a duplicate will be suppressed at the store.
        if effect.evidence.get("applied") is True:
            return Reconciliation(
                Verdict.NEEDS_COMPENSATION,
                "write applied but reported failure; compensating",
                compensate=action.side_effect is SideEffect.REVERSIBLE_WRITE,
            )
        return Reconciliation(
            Verdict.RETRYABLE, "transient write failure; retry is idempotency-protected"
        )

    if effect.retry_class in (RetryClass.DETERMINISTIC, RetryClass.POLICY_BLOCKED):
        return Reconciliation(
            Verdict.BENIGN_FAILURE, f"{effect.retry_class.value} failure; no effect produced"
        )

    return Reconciliation(Verdict.MISMATCH, effect.error or "unclassified failure")
