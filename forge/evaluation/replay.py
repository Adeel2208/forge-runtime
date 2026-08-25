"""Replay and deterministic diffing (spec §19).

Replay reconstructs a run's trajectory from the event log and re-executes it
against a provider scripted from the *recorded* proposals. If the runtime is
deterministic, the replayed trajectory is identical; if a change altered
behaviour, `ReplayDiff` names the first step where the two diverge.

We compare redacted action/state summaries, never private reasoning - which is
both the §19 requirement and the only thing that is actually stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forge.core.enums import EventType
from forge.core.events import Event
from forge.llm.mock import MockProvider, ScriptedTurn

__all__ = ["ReplayDiff", "TrajectoryStep", "replay_run", "script_from_trajectory", "trajectory_of"]


@dataclass
class TrajectoryStep:
    """One comparable step. Only decision-relevant fields are included."""

    index: int
    proposal_kind: str = ""
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    decision: str | None = None
    effect_ok: bool | None = None
    verdict: str | None = None

    def signature(self) -> tuple[Any, ...]:
        """What must match for two steps to count as the same behaviour.

        Latency, timestamps and ids are excluded on purpose: they differ
        between runs without indicating a behavioural change.
        """
        return (
            self.index,
            self.proposal_kind,
            self.tool,
            tuple(sorted((k, str(v)) for k, v in self.arguments.items())),
            self.decision,
            self.effect_ok,
            self.verdict,
        )

    def describe(self) -> str:
        bits = [f"step {self.index}", self.proposal_kind]
        if self.tool:
            bits.append(f"tool={self.tool}({_short(self.arguments)})")
        if self.decision:
            bits.append(f"decision={self.decision}")
        if self.effect_ok is not None:
            bits.append(f"ok={self.effect_ok}")
        return " ".join(bits)


def trajectory_of(events: list[Event]) -> list[TrajectoryStep]:
    """Fold an event log into an ordered, comparable trajectory."""
    steps: dict[int, TrajectoryStep] = {}

    def slot(index: int | None) -> TrajectoryStep | None:
        if index is None:
            return None
        return steps.setdefault(index, TrajectoryStep(index=index))

    for ev in events:
        step = slot(ev.step_index)
        if step is None:
            continue
        match ev.type:
            case EventType.PROPOSAL_RECEIVED:
                step.proposal_kind = str(ev.payload.get("kind", ""))
                step.tool = ev.payload.get("tool")
                step.arguments = dict(ev.payload.get("arguments") or {})
            case EventType.POLICY_DECIDED:
                step.decision = str(ev.payload.get("decision"))
            case EventType.EFFECT_OBSERVED | EventType.EFFECT_REUSED:
                step.effect_ok = bool(ev.payload.get("ok"))
            case EventType.EFFECT_RECONCILED:
                step.verdict = str(ev.payload.get("verdict"))
            case _:
                pass

    return [steps[i] for i in sorted(steps)]


def script_from_trajectory(events: list[Event]) -> MockProvider:
    """Build a provider that re-serves exactly the proposals a run received."""
    turns: list[ScriptedTurn] = []
    for ev in events:
        if ev.type is not EventType.PROPOSAL_RECEIVED:
            continue
        proposal: dict[str, Any] = {"kind": ev.payload.get("kind")}
        for key in ("tool", "arguments", "answer", "rationale_summary"):
            value = ev.payload.get(key)
            if value is not None:
                proposal[key] = value
        turns.append(ScriptedTurn(proposal=proposal))
    return MockProvider(turns or [ScriptedTurn(proposal={"kind": "ANSWER", "answer": ""})],
                        name="replay", model="replay-1")


@dataclass
class ReplayDiff:
    """The outcome of comparing two trajectories."""

    identical: bool
    first_divergence: int | None = None
    left: str = ""
    right: str = ""
    left_len: int = 0
    right_len: int = 0

    def render(self) -> str:
        if self.identical:
            return f"identical: {self.left_len} steps matched"
        if self.first_divergence is None:
            return f"length differs: {self.left_len} vs {self.right_len} steps"
        return (
            f"diverged at step {self.first_divergence}\n"
            f"  baseline: {self.left}\n"
            f"  replay:   {self.right}"
        )


def compare(left: list[TrajectoryStep], right: list[TrajectoryStep]) -> ReplayDiff:
    for a, b in zip(left, right, strict=False):
        if a.signature() != b.signature():
            return ReplayDiff(
                identical=False,
                first_divergence=a.index,
                left=a.describe(),
                right=b.describe(),
                left_len=len(left),
                right_len=len(right),
            )
    if len(left) != len(right):
        return ReplayDiff(identical=False, left_len=len(left), right_len=len(right))
    return ReplayDiff(identical=True, left_len=len(left), right_len=len(right))


async def replay_run(run_id: str, *, store: Any, build_runtime: Any) -> ReplayDiff:
    """Re-execute a recorded run against its own proposals and diff the result.

    `build_runtime(provider, store)` must return a fresh `AgentRuntime`; the
    caller supplies it so replay does not need to guess how the original
    runtime was assembled.
    """
    from forge.core.contracts import TaskSpec

    original = await store.read(run_id)
    if not original:
        raise ValueError(f"no events recorded for run {run_id!r}")

    created = next((e for e in original if e.type is EventType.RUN_CREATED), None)
    if created is None:
        raise ValueError(f"run {run_id!r} has no RUN_CREATED event")

    baseline = trajectory_of(original)
    provider = script_from_trajectory(original)
    runtime = build_runtime(provider)

    task = TaskSpec(
        goal=created.payload.get("goal", ""),
        max_steps=int(created.payload.get("max_steps", 24)),
        tools=list(created.payload.get("tools", [])),
    )
    result = await runtime.start(task)
    replayed = trajectory_of(await runtime.store.read(result.run_id))

    return compare(baseline, replayed)


def _short(value: dict[str, Any], limit: int = 60) -> str:
    text = ", ".join(f"{k}={v}" for k, v in sorted(value.items()))
    return text if len(text) <= limit else text[:limit] + "..."
