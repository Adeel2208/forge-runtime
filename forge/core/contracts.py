"""Typed contracts for every runtime boundary (spec §6).

Design rule: a `Proposal` is what the *model* said; an `Action` is what the
*runtime* authorized. They are separate types on purpose - you cannot pass a
Proposal to the dispatcher, so "the model's output was executed directly" is a
compile-time impossibility rather than a code-review question.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forge.core.enums import (
    Decision,
    Phase,
    ProposalKind,
    RetryClass,
    RiskClass,
    RunStatus,
    SideEffect,
)
from forge.ids import new_id

__all__ = [
    "Action",
    "Checkpoint",
    "ContextView",
    "Effect",
    "Evaluation",
    "Frozen",
    "Permit",
    "PolicyDecision",
    "Proposal",
    "Run",
    "Step",
    "TaskSpec",
    "Usage",
]


class Frozen(BaseModel):
    """Immutable, strict base. Unknown fields are an error, not a shrug."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)


class Usage(Frozen):
    """Token and cost accounting for one model call (spec §15)."""

    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            usd=round(self.usd + other.usd, 10),
        )


class TaskSpec(Frozen):
    """What the caller wants done. The only untrusted free text at the top."""

    goal: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    max_steps: int = 24
    tools: list[str] = Field(default_factory=list)
    """Allow-list of tool names. Empty means *no* tools - deny by default."""

    success_check: str | None = None
    """Optional name of a registered evaluator for automatic scoring."""


class Run(Frozen):
    id: str = Field(default_factory=lambda: new_id("run"))
    task: TaskSpec
    status: RunStatus = RunStatus.PENDING
    parent_run_id: str | None = None
    policy_version: str = "unset"
    component_versions: dict[str, str] = Field(default_factory=dict)
    created_at: datetime | None = None
    ended_at: datetime | None = None


class ContextView(Frozen):
    """A bounded, policy-aware view of state handed to one model call (spec §10).

    `snapshot_hash` makes the view reproducible: replay can assert that the
    same inputs produced the same view before comparing model behaviour.
    """

    step_id: str
    system: str
    messages: list[dict[str, str]]
    tool_schemas: list[dict[str, Any]] = Field(default_factory=list)
    token_estimate: int = 0
    token_budget: int = 0
    included: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    """What the compiler left out, and why - context decisions are auditable."""

    snapshot_hash: str = ""


class Proposal(Frozen):
    """Exactly what the model returned, before any runtime interpretation.

    Stored verbatim so that "why did the agent do this?" is answerable from the
    log without re-running anything.
    """

    kind: ProposalKind
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    answer: str | None = None
    rationale_summary: str | None = None
    """A short, redacted summary. Never raw chain-of-thought (spec §19)."""

    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _kind_requires_payload(self) -> Proposal:
        if self.kind is ProposalKind.TOOL_CALL and not self.tool:
            raise ValueError("TOOL_CALL proposal must name a tool")
        if self.kind is ProposalKind.ANSWER and self.answer is None:
            raise ValueError("ANSWER proposal must carry an answer")
        return self


class PolicyDecision(Frozen):
    """The trust plane's ruling, always recorded - allow and deny alike."""

    decision: Decision
    reason: str
    policy_version: str
    capability: str | None = None
    risk: RiskClass = RiskClass.LOW
    obligations: list[str] = Field(default_factory=list)
    """Conditions the dispatcher must honour, e.g. ``dry_run``."""


class Permit(Frozen):
    """A scoped, single-use authorization for one concrete action (spec §6).

    The dispatcher refuses to act without one. Permits are bound to the action
    hash, so a permit issued for a cheap action cannot be replayed against an
    expensive one.
    """

    id: str = Field(default_factory=lambda: new_id("permit"))
    run_id: str
    step_id: str
    capability: str
    action_hash: str
    side_effect: SideEffect
    issued_at: datetime | None = None
    expires_after_step: bool = True


class Action(Frozen):
    """An authorized operation. Only the runtime can mint one."""

    id: str = Field(default_factory=lambda: new_id("act"))
    run_id: str
    step_id: str
    tool: str
    arguments: dict[str, Any]
    side_effect: SideEffect
    idempotency_key: str
    permit_id: str
    dry_run: bool = False
    timeout_s: float = 30.0
    attempt: int = 1


class Effect(Frozen):
    """The observed external result of an action (spec §6)."""

    action_id: str
    idempotency_key: str
    ok: bool
    output: Any = None
    error: str | None = None
    retry_class: RetryClass | None = None
    latency_ms: int = 0
    evidence: dict[str, Any] = Field(default_factory=dict)
    """Provider-side proof of the effect: request ids, ETags, row counts."""

    reused: bool = False
    """True when idempotency short-circuited a real dispatch on resume."""


class Step(Frozen):
    id: str = Field(default_factory=lambda: new_id("step"))
    run_id: str
    index: int
    phase: Phase = Phase.VIEW
    proposal: Proposal | None = None
    decision: PolicyDecision | None = None
    action: Action | None = None
    effect: Effect | None = None
    usage: Usage = Field(default_factory=Usage)
    started_at: datetime | None = None
    ended_at: datetime | None = None


class Checkpoint(Frozen):
    """A resumable snapshot (spec §9).

    `last_seq` is the watermark: on resume the runtime restores this state and
    replays only events after it, which is what keeps resume O(recent) rather
    than O(history).
    """

    id: str = Field(default_factory=lambda: new_id("ckpt"))
    run_id: str
    step_index: int
    last_seq: int
    state: dict[str, Any]
    context_digest: str = ""
    created_at: datetime | None = None
    kind: Literal["periodic", "semantic"] = "semantic"


class Evaluation(Frozen):
    """Step-, trajectory- or run-level score (spec §17)."""

    run_id: str
    step_index: int | None = None
    scores: dict[str, float] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    notes: str = ""
