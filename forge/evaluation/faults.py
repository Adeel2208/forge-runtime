"""Deterministic fault injection (spec §18).

Resilience claims are worthless without a way to reproduce the failure. Every
injector is seeded, so "recovered from a worker crash at step 3" is a fact
someone else can re-run, not an anecdote.

Faults fire at named hooks the runtime already calls (`before_model`,
`before_dispatch`, `after_dispatch`, `phase`), which keeps the injector out of
the production path entirely - there is no `if self.testing:` anywhere in
`forge/runtime/`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from forge.errors import DeterministicError, TransientError

__all__ = ["FaultClass", "FaultInjector", "FaultSpec", "InjectedToolFailure"]


class FaultClass(StrEnum):
    """The eleven classes from spec §18."""

    LLM_TIMEOUT = "llm_timeout"
    MALFORMED_OUTPUT = "malformed_output"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_INVALID_SCHEMA = "tool_invalid_schema"
    DB_INTERRUPTION = "db_interruption"
    WORKER_CRASH = "worker_crash"
    CONTEXT_OVERFLOW = "context_overflow"
    REPEATED_ACTION_LOOP = "repeated_action_loop"
    POLICY_DENIAL = "policy_denial"
    STALE_CHECKPOINT = "stale_checkpoint"
    EFFECT_MISMATCH = "effect_mismatch"

    NONE = "none"
    """The control arm. Every benchmark needs one."""


class InjectedToolFailure(TransientError):
    """Raised inside a tool call to simulate a remote-side failure."""


@dataclass
class FaultSpec:
    """One scheduled fault."""

    kind: FaultClass
    at_step: int | None = None
    """Fire at this step index. None means "governed by probability"."""

    probability: float = 1.0
    max_fires: int = 1

    def __post_init__(self) -> None:
        if not 0.0 <= self.probability <= 1.0:
            raise ValueError("probability must be in [0, 1]")


@dataclass
class FaultInjector:
    """Fires scheduled faults at runtime hooks. Seeded, therefore reproducible."""

    specs: list[FaultSpec] = field(default_factory=list)
    seed: int = 1729
    fired: list[dict[str, Any]] = field(default_factory=list)
    _rng: random.Random = field(init=False)
    _counts: dict[FaultClass, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    @classmethod
    def none(cls) -> FaultInjector:
        return cls(specs=[])

    @classmethod
    def single(
        cls, kind: FaultClass, *, at_step: int | None = 1, seed: int = 1729
    ) -> FaultInjector:
        """One fault, once.

        ``at_step=None`` means "at the next opportunity, whenever that is",
        which is the robust way to target a resumed run: step indices shift
        after a resume, and a reused effect never reaches the dispatch hook
        at all, so pinning an exact number silently stops firing.
        """
        if kind is FaultClass.NONE:
            return cls.none()
        return cls(specs=[FaultSpec(kind=kind, at_step=at_step)], seed=seed)

    # -- the hook the runtime calls ---------------------------------------

    async def check(self, hook: str, **ctx: Any) -> None:
        """Fire any fault scheduled for this hook. May raise."""
        step = int(ctx.get("step") or 0)
        for spec in self.specs:
            if not self._should_fire(spec, step):
                continue
            target_hook = _HOOK_FOR[spec.kind]
            if target_hook != hook:
                continue
            self._counts[spec.kind] = self._counts.get(spec.kind, 0) + 1
            self.fired.append({"kind": spec.kind.value, "hook": hook, "step": step})
            self._raise(spec.kind, ctx)

    def _should_fire(self, spec: FaultSpec, step: int) -> bool:
        if self._counts.get(spec.kind, 0) >= spec.max_fires:
            return False
        if spec.at_step is not None and step != spec.at_step:
            return False
        if spec.probability >= 1.0:
            return True
        return self._rng.random() < spec.probability

    def _raise(self, kind: FaultClass, ctx: dict[str, Any]) -> None:
        from forge.runtime.loop import SimulatedCrash

        match kind:
            case FaultClass.WORKER_CRASH:
                raise SimulatedCrash(f"injected worker crash at step {ctx.get('step')}")
            case FaultClass.LLM_TIMEOUT:
                raise TransientError("injected LLM timeout", provider="injected")
            case FaultClass.TOOL_TIMEOUT:
                raise InjectedToolFailure("injected tool timeout")
            case FaultClass.DB_INTERRUPTION:
                raise TransientError("injected event-store interruption")
            case FaultClass.TOOL_INVALID_SCHEMA:
                raise DeterministicError("injected invalid tool response schema")
            case FaultClass.POLICY_DENIAL:
                from forge.errors import PolicyDenied

                raise PolicyDenied(
                    "injected policy denial", reason="fault injection",
                    policy_version="injected",
                )
            case FaultClass.EFFECT_MISMATCH:
                from forge.errors import EffectMismatch

                raise EffectMismatch("injected effect mismatch")
            case FaultClass.CONTEXT_OVERFLOW:
                raise DeterministicError("injected context overflow")
            case _:
                return

    # -- faults expressed as data, not exceptions -------------------------
    #
    # MALFORMED_OUTPUT, REPEATED_ACTION_LOOP and STALE_CHECKPOINT are not
    # raised: they are induced by the harness shaping the mock script or the
    # checkpoint, because that is how they actually occur in the wild.

    @staticmethod
    def shapes_script(kind: FaultClass) -> bool:
        return kind in (FaultClass.MALFORMED_OUTPUT, FaultClass.REPEATED_ACTION_LOOP)

    @property
    def fire_count(self) -> int:
        return len(self.fired)


# Where each fault is injected. Placement is a correctness question, not a
# convenience: a fault must enter the runtime at the point its real-world
# counterpart would, or the benchmark measures the harness instead of the
# system. A tool timeout raised outside the tool call, for instance, would skip
# effect recording and reconciliation entirely and look far more fatal than it is.
_HOOK_FOR: dict[FaultClass, str] = {
    FaultClass.LLM_TIMEOUT: "before_model",
    FaultClass.CONTEXT_OVERFLOW: "before_model",
    FaultClass.WORKER_CRASH: "before_dispatch",   # death *before* the effect
    FaultClass.TOOL_TIMEOUT: "in_tool",           # inside the tool boundary
    FaultClass.TOOL_INVALID_SCHEMA: "in_tool",
    FaultClass.EFFECT_MISMATCH: "after_dispatch",
    FaultClass.DB_INTERRUPTION: "before_dispatch",
    FaultClass.POLICY_DENIAL: "authorize",        # where real denials are decided
    FaultClass.MALFORMED_OUTPUT: "__script__",
    FaultClass.REPEATED_ACTION_LOOP: "__script__",
    FaultClass.STALE_CHECKPOINT: "__harness__",
    FaultClass.NONE: "__never__",
}
