"""The runtime's closed vocabularies.

These are `StrEnum` so they serialise to readable JSON in the event log without
a codec, and compare equal to their wire form when reading old events back.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "TERMINAL_PHASES",
    "Decision",
    "EventType",
    "Phase",
    "ProposalKind",
    "RetryClass",
    "RiskClass",
    "RunStatus",
    "SideEffect",
]


class Phase(StrEnum):
    """The lifecycle from spec §5.

    An explicit state machine, not an unconstrained ReAct loop: the set of
    legal successors is declared in `forge.runtime.machine.TRANSITIONS` and
    property-tested, so "reached COMMIT without AUTHORIZE" is unrepresentable.
    """

    BOOT = "BOOT"
    VIEW = "VIEW"
    PROPOSE = "PROPOSE"
    VALIDATE = "VALIDATE"
    AUTHORIZE = "AUTHORIZE"
    DISPATCH = "DISPATCH"
    OBSERVE = "OBSERVE"
    RECONCILE = "RECONCILE"
    COMMIT = "COMMIT"
    EVALUATE = "EVALUATE"
    CONTINUE = "CONTINUE"
    HANDOFF = "HANDOFF"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


TERMINAL_PHASES: frozenset[Phase] = frozenset(
    {Phase.COMPLETE, Phase.FAILED, Phase.ABORTED, Phase.HANDOFF}
)


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"        # awaiting human approval
    INTERRUPTED = "INTERRUPTED"    # worker died; resumable
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class ProposalKind(StrEnum):
    """What the model asked the runtime to do (spec §5 PROPOSE)."""

    ANSWER = "ANSWER"
    TOOL_CALL = "TOOL_CALL"
    DELEGATE = "DELEGATE"
    MEMORY_OP = "MEMORY_OP"
    CONTROL = "CONTROL"


class SideEffect(StrEnum):
    """Spec §12. Drives authorization strictness and compensation strategy."""

    READ = "READ"
    REVERSIBLE_WRITE = "REVERSIBLE_WRITE"
    IRREVERSIBLE_WRITE = "IRREVERSIBLE_WRITE"


class RiskClass(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RetryClass(StrEnum):
    """Spec §9. Determines the recovery strategy for a failure."""

    TRANSIENT = "TRANSIENT"              # retry unchanged, with backoff
    DETERMINISTIC = "DETERMINISTIC"      # retry only with changed input
    POLICY_BLOCKED = "POLICY_BLOCKED"    # never retried; escalate or abandon
    UNRECOVERABLE = "UNRECOVERABLE"      # fail the run


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class EventType(StrEnum):
    """The append-only log's alphabet.

    Canonical state is the fold of these events; nothing else may mutate it.
    """

    RUN_CREATED = "RUN_CREATED"
    RUN_RESUMED = "RUN_RESUMED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"
    RUN_ABORTED = "RUN_ABORTED"

    STEP_STARTED = "STEP_STARTED"
    PHASE_ENTERED = "PHASE_ENTERED"

    CONTEXT_COMPILED = "CONTEXT_COMPILED"
    MODEL_CALLED = "MODEL_CALLED"
    PROPOSAL_RECEIVED = "PROPOSAL_RECEIVED"
    PROPOSAL_REJECTED = "PROPOSAL_REJECTED"

    POLICY_DECIDED = "POLICY_DECIDED"
    PERMIT_ISSUED = "PERMIT_ISSUED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_RESOLVED = "APPROVAL_RESOLVED"

    ACTION_DISPATCHED = "ACTION_DISPATCHED"
    EFFECT_OBSERVED = "EFFECT_OBSERVED"
    EFFECT_REUSED = "EFFECT_REUSED"          # idempotency hit: no second effect
    EFFECT_RECONCILED = "EFFECT_RECONCILED"
    COMPENSATION_APPLIED = "COMPENSATION_APPLIED"

    STEP_COMMITTED = "STEP_COMMITTED"
    CHECKPOINT_WRITTEN = "CHECKPOINT_WRITTEN"
    EVALUATION_RECORDED = "EVALUATION_RECORDED"

    FAULT_INJECTED = "FAULT_INJECTED"
    ERROR_RAISED = "ERROR_RAISED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    LOOP_DETECTED = "LOOP_DETECTED"

    # -- knowledge layer (docs/adr/0007) ----------------------------------
    # Shared knowledge between runs. These live in the same log and use the
    # same idempotency index as everything above; `project()` ignores them,
    # and `forge.knowledge.projection` folds them separately. Adding members
    # is safe in both directions because both folds are total.
    NOTE_PROPOSED = "NOTE_PROPOSED"
    ATTESTATION_RECORDED = "ATTESTATION_RECORDED"
    ATTESTATION_RETRACTED = "ATTESTATION_RETRACTED"
    NOTE_QUARANTINED = "NOTE_QUARANTINED"
    NOTE_RELEASED = "NOTE_RELEASED"
    NOTES_MERGED = "NOTES_MERGED"
    NOTE_REANCHORED = "NOTE_REANCHORED"
    ADVERSARIAL_RETEST_RECORDED = "ADVERSARIAL_RETEST_RECORDED"
