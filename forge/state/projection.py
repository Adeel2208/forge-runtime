"""Materialize canonical state by folding the event log.

This is the single definition of "what is true about a run". Both the live
runtime and the resume path call `project()`, so a resumed run cannot diverge
from a run that never crashed - there is no second code path to drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forge.core.contracts import Usage
from forge.core.enums import EventType, RunStatus
from forge.core.events import Event

__all__ = ["RunState", "project"]


@dataclass
class RunState:
    """The fold of a run's events. Pure data - no behaviour, no I/O."""

    run_id: str = ""
    status: RunStatus = RunStatus.PENDING
    step_index: int = 0
    last_seq: int = 0
    goal: str = ""
    answer: str | None = None
    usage: Usage = field(default_factory=Usage)
    steps_committed: int = 0
    observations: list[dict[str, Any]] = field(default_factory=list)
    """Tool results the next context view may draw on."""

    failures: list[dict[str, Any]] = field(default_factory=list)
    """Previous failed attempts - deliberately retained (spec §10)."""

    completed_effects: dict[str, dict[str, Any]] = field(default_factory=dict)
    """idempotency_key -> effect payload. The duplicate-suppression table."""

    action_fingerprints: list[str] = field(default_factory=list)
    """Ordered action hashes, for loop detection (spec §7)."""

    denials: list[dict[str, Any]] = field(default_factory=list)
    resumes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "step_index": self.step_index,
            "last_seq": self.last_seq,
            "goal": self.goal,
            "answer": self.answer,
            "usage": self.usage.model_dump(),
            "steps_committed": self.steps_committed,
            "observations": self.observations,
            "failures": self.failures,
            "completed_effects": self.completed_effects,
            "action_fingerprints": self.action_fingerprints,
            "denials": self.denials,
            "resumes": self.resumes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        return cls(
            run_id=data.get("run_id", ""),
            status=RunStatus(data.get("status", RunStatus.PENDING)),
            step_index=int(data.get("step_index", 0)),
            last_seq=int(data.get("last_seq", 0)),
            goal=data.get("goal", ""),
            answer=data.get("answer"),
            usage=Usage(**data.get("usage", {})),
            steps_committed=int(data.get("steps_committed", 0)),
            observations=list(data.get("observations", [])),
            failures=list(data.get("failures", [])),
            completed_effects=dict(data.get("completed_effects", {})),
            action_fingerprints=list(data.get("action_fingerprints", [])),
            denials=list(data.get("denials", [])),
            resumes=int(data.get("resumes", 0)),
        )


def project(events: list[Event], base: RunState | None = None) -> RunState:
    """Fold events onto `base` (a checkpoint's state, or a fresh run).

    Folding is total: an unrecognised event type advances `last_seq` and is
    otherwise ignored, so an older runtime can read a newer log without
    crashing. Forward-compatibility is cheap here and expensive to retrofit.
    """
    state = base or RunState()

    for ev in events:
        state.last_seq = max(state.last_seq, ev.seq)
        payload = ev.payload

        match ev.type:
            case EventType.RUN_CREATED:
                state.run_id = ev.run_id
                state.goal = payload.get("goal", "")
                state.status = RunStatus.RUNNING

            case EventType.RUN_RESUMED:
                state.resumes += 1
                state.status = RunStatus.RUNNING

            case EventType.STEP_STARTED:
                state.step_index = int(payload.get("index", state.step_index))

            case EventType.MODEL_CALLED:
                usage = payload.get("usage") or {}
                state.usage = state.usage + Usage(**usage)

            case EventType.PROPOSAL_REJECTED:
                state.failures.append(
                    {
                        "step": ev.step_index,
                        "kind": "proposal_invalid",
                        "detail": payload.get("error", ""),
                    }
                )

            case EventType.POLICY_DECIDED:
                if payload.get("decision") == "DENY":
                    state.denials.append(
                        {
                            "step": ev.step_index,
                            "capability": payload.get("capability"),
                            "reason": payload.get("reason", ""),
                        }
                    )

            case EventType.ACTION_DISPATCHED:
                fingerprint = payload.get("fingerprint")
                if fingerprint:
                    state.action_fingerprints.append(str(fingerprint))

            case EventType.EFFECT_OBSERVED | EventType.EFFECT_REUSED:
                key = ev.idempotency_key
                if key:
                    state.completed_effects[key] = payload
                if payload.get("ok"):
                    state.observations.append(
                        {
                            "step": ev.step_index,
                            "tool": payload.get("tool"),
                            "output": payload.get("output"),
                            # A suppressed duplicate and a fresh success look
                            # identical to the model unless we say otherwise.
                            "reused": ev.type is EventType.EFFECT_REUSED,
                        }
                    )
                else:
                    state.failures.append(
                        {
                            "step": ev.step_index,
                            "kind": "tool_failed",
                            "tool": payload.get("tool"),
                            "detail": payload.get("error", ""),
                        }
                    )

            case EventType.STEP_COMMITTED:
                state.steps_committed += 1

            case EventType.RUN_COMPLETED:
                state.status = RunStatus.COMPLETED
                state.answer = payload.get("answer")

            case EventType.RUN_FAILED:
                state.status = RunStatus.FAILED

            case EventType.RUN_ABORTED:
                state.status = RunStatus.ABORTED

            case _:
                pass  # forward-compatible: unknown events only advance the seq

    return state
