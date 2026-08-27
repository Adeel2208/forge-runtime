"""The agent runtime: one explicit pass through the lifecycle per step.

Read this file as the answer to "what actually happens between the model
speaking and the world changing". Every phase boundary calls
`assert_transition`, so the machine table in `machine.py` is load-bearing
rather than documentation, and every phase writes an event, so the log is a
complete account of the run rather than a sampling of it.

Crash-safety invariant, stated plainly:

    An effect is recorded in the same durable append that claims its
    idempotency key. Therefore, after any crash, either the effect is in the
    log (and resume reuses it) or it is not (and resume performs it) - never
    both, and never neither-but-applied.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from forge.clock import Clock, SystemClock
from forge.context.compiler import ContextCompiler
from forge.core.contracts import (
    Action,
    Checkpoint,
    Effect,
    PolicyDecision,
    Proposal,
    Run,
    TaskSpec,
    Usage,
)
from forge.core.enums import (
    Decision,
    EventType,
    Phase,
    ProposalKind,
    RetryClass,
    RunStatus,
)
from forge.core.events import NewEvent
from forge.errors import (
    ForgeError,
    LoopDetected,
    PolicyDenied,
    UnrecoverableError,
)
from forge.ids import content_hash, idempotency_key, new_id
from forge.llm.base import ModelRequest
from forge.llm.gateway import LLMGateway, RouteAttempt
from forge.runtime.loopdetect import LoopDetector
from forge.runtime.machine import assert_transition
from forge.runtime.reconcile import Verdict, reconcile
from forge.runtime.recovery import RetryPolicy, classify
from forge.security.capabilities import PermitBook
from forge.security.policy import PolicyEngine
from forge.state.projection import RunState, project
from forge.state.store import EventStore
from forge.telemetry.metrics import Metrics
from forge.telemetry.tracer import Tracer, redact
from forge.tools.registry import ToolRegistry

__all__ = ["PROPOSAL_SCHEMA", "AgentRuntime", "RunResult", "RuntimeConfig", "SimulatedCrash"]


class SimulatedCrash(BaseException):
    """Injected worker death.

    Inherits `BaseException` deliberately: it must not be swallowed by the
    runtime's own `except Exception` recovery paths, because a real SIGKILL
    would not be caught either. Simulating a crash that the code can catch
    would prove nothing.
    """


# Structured-output schema handed to providers that support enforcement.
PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["TOOL_CALL", "ANSWER"]},
        "tool": {"type": "string"},
        "arguments": {"type": "object"},
        "answer": {"type": "string"},
        "rationale_summary": {"type": "string"},
    },
    "required": ["kind"],
}

ApprovalFn = Callable[[Action], Awaitable[bool]]


@dataclass
class RuntimeConfig:
    max_steps: int = 24
    token_budget: int = 3000
    max_repairs_per_step: int = 2
    """Reprompts allowed when a proposal fails validation."""

    checkpoint_every: int = 1
    max_dispatch_attempts: int = 3
    seed: int = 1729
    """Seeds jitter so benchmark latency figures are comparable."""

    auto_approve: bool = False
    """Demo/benchmark convenience. Never default-on in a real deployment."""


@dataclass
class RunResult:
    run_id: str
    status: RunStatus
    answer: str | None
    steps: int
    usage: Usage
    duration_ms: int
    resumed: bool = False
    error: str | None = None
    duplicate_effects: int = 0
    """Effects observed more than once. The recovery tests assert this is 0."""

    denials: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is RunStatus.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "answer": self.answer,
            "steps": self.steps,
            "usage": self.usage.model_dump(),
            "duration_ms": self.duration_ms,
            "resumed": self.resumed,
            "error": self.error,
            "duplicate_effects": self.duplicate_effects,
            "denials": self.denials,
        }


class AgentRuntime:
    """Drives one run through the lifecycle, durably."""

    def __init__(
        self,
        *,
        store: EventStore,
        gateway: LLMGateway,
        registry: ToolRegistry,
        policy: PolicyEngine,
        compiler: ContextCompiler | None = None,
        config: RuntimeConfig | None = None,
        tracer: Tracer | None = None,
        metrics: Metrics | None = None,
        clock: Clock | None = None,
        faults: Any = None,
        approval: ApprovalFn | None = None,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.registry = registry
        self.policy = policy
        self.config = config or RuntimeConfig()
        self.compiler = compiler or ContextCompiler(token_budget=self.config.token_budget)
        self.tracer = tracer or Tracer()
        self.metrics = metrics or Metrics()
        self.clock = clock or SystemClock()
        self.faults = faults
        self.approval = approval
        self.permits = PermitBook()
        self.retry = RetryPolicy()
        self.detector = LoopDetector()
        self._rng = random.Random(self.config.seed)
        self._duplicates = 0
        self._last_seq = 0
        """Highest event seq this worker has appended. Checkpoint watermark."""

    # ------------------------------------------------------------------ API

    async def start(self, task: TaskSpec, *, run_id: str | None = None) -> RunResult:
        """Begin a new run."""
        run = Run(
            id=run_id or new_id("run"),
            task=task,
            policy_version=self.policy.version,
            created_at=self.clock.now(),
        )
        await self._emit(
            EventType.RUN_CREATED,
            run.id,
            payload={
                "goal": task.goal,
                "max_steps": task.max_steps,
                "tools": task.tools,
                "policy_version": self.policy.version,
            },
        )
        state = RunState(run_id=run.id, goal=task.goal, status=RunStatus.RUNNING)
        return await self._drive(run, state, resumed=False)

    async def resume(self, run_id: str) -> RunResult:
        """Restore a run from its latest checkpoint and continue.

        Restores canonical state *and* the effect table, which is what makes
        the next dispatch idempotency-aware rather than blindly re-executing.
        """
        checkpoint = await self.store.latest_checkpoint(run_id)
        if checkpoint is None:
            base, after = RunState(run_id=run_id), 0
        else:
            base, after = RunState.from_dict(checkpoint.state), checkpoint.last_seq

        tail = await self.store.read(run_id, after_seq=after)
        state = project(tail, base)
        if not state.run_id:
            state.run_id = run_id
        self._last_seq = state.last_seq

        if state.status in (RunStatus.COMPLETED, RunStatus.ABORTED):
            return RunResult(
                run_id=run_id,
                status=state.status,
                answer=state.answer,
                steps=state.steps_committed,
                usage=state.usage,
                duration_ms=0,
                resumed=True,
            )

        head = await self.store.read(run_id)
        created = next((e for e in head if e.type is EventType.RUN_CREATED), None)
        if created is None:
            raise UnrecoverableError("cannot resume: no RUN_CREATED event", run_id=run_id)

        task = TaskSpec(
            goal=created.payload.get("goal", state.goal),
            max_steps=int(created.payload.get("max_steps", self.config.max_steps)),
            tools=list(created.payload.get("tools", [])),
        )
        run = Run(id=run_id, task=task, policy_version=self.policy.version)

        # Rebuild loop-detector history so a crash does not reset the bound -
        # otherwise "crash and resume" would be a way to loop forever.
        self.detector.fingerprints = list(state.action_fingerprints)

        await self._emit(
            EventType.RUN_RESUMED,
            run_id,
            payload={
                "from_checkpoint": checkpoint.id if checkpoint else None,
                "from_seq": after,
                "known_effects": len(state.completed_effects),
            },
        )
        return await self._drive(run, state, resumed=True)

    # --------------------------------------------------------------- driver

    async def _drive(self, run: Run, state: RunState, *, resumed: bool) -> RunResult:
        started = time.monotonic()
        budget = self.policy.bundle.budget
        budget.steps = state.steps_committed
        budget.tokens = state.usage.total_tokens
        phase = Phase.BOOT
        error: str | None = None
        max_steps = min(run.task.max_steps, self.config.max_steps)

        with self.tracer.span("run", run_id=run.id, goal=run.task.goal, resumed=resumed):
            try:
                # The VIEW transition belongs *inside* the step, after
                # STEP_STARTED - see `_run_step`. Entering it here would tag
                # the event with the previous step's index.
                while state.step_index < max_steps:
                    state.step_index += 1
                    step_id = f"{run.id}:s{state.step_index}"
                    budget.steps = state.step_index
                    budget.elapsed_s = time.monotonic() - started
                    budget.check()

                    await self._emit(
                        EventType.STEP_STARTED,
                        run.id,
                        step_id=step_id,
                        step_index=state.step_index,
                        payload={"index": state.step_index},
                    )

                    with self.tracer.span("step", index=state.step_index, step_id=step_id):
                        phase, outcome = await self._run_step(
                            run, state, step_id, phase
                        )

                    if outcome == "complete":
                        phase = await self._goto(run.id, phase, Phase.COMPLETE, state.step_index)
                        state.status = RunStatus.COMPLETED
                        break
                    if outcome == "failed":
                        phase = await self._goto(run.id, phase, Phase.FAILED, state.step_index)
                        state.status = RunStatus.FAILED
                        # Carry the reason out. A run that failed without
                        # saying why forces the operator into the event log
                        # for something the result should have told them.
                        if state.failures:
                            last = state.failures[-1]
                            error = f"{last.get('kind')}: {last.get('detail')}"
                        await self._emit(
                            EventType.RUN_FAILED, run.id,
                            payload={"error": error or "step failed"},
                        )
                        break

                else:
                    # Ran out of steps without answering.
                    state.status = RunStatus.FAILED
                    error = f"step budget exhausted after {max_steps} steps"
                    await self._emit(
                        EventType.RUN_FAILED, run.id, payload={"error": error}
                    )

            except SimulatedCrash:
                # Do not write anything. A killed worker gets no epilogue - the
                # log must look exactly as it would after a real SIGKILL.
                raise

            except (LoopDetected, PolicyDenied) as exc:
                state.status = RunStatus.FAILED
                error = str(exc)
                await self._emit(EventType.RUN_FAILED, run.id, payload={"error": error})

            except ForgeError as exc:
                state.status = RunStatus.FAILED
                error = str(exc)
                await self._emit(
                    EventType.RUN_FAILED,
                    run.id,
                    payload={"error": error, "class": classify(exc).value},
                )

        if state.status is RunStatus.COMPLETED:
            await self._emit(
                EventType.RUN_COMPLETED, run.id, payload={"answer": state.answer}
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        self.metrics.observe("forge_run_duration_ms", duration_ms, status=state.status.value)
        self.metrics.inc("forge_runs_total", status=state.status.value)

        return RunResult(
            run_id=run.id,
            status=state.status,
            answer=state.answer,
            steps=state.steps_committed,
            usage=state.usage,
            duration_ms=duration_ms,
            resumed=resumed,
            error=error,
            duplicate_effects=self._duplicates,
            denials=list(state.denials),
            trace=self.tracer.to_list(),
        )

    # ------------------------------------------------------------ one step

    async def _run_step(
        self, run: Run, state: RunState, step_id: str, phase: Phase
    ) -> tuple[Phase, str]:
        """Run one VIEW..EVALUATE pass. Returns the phase and an outcome word."""
        proposal: Proposal | None = None

        # -- VIEW / PROPOSE / VALIDATE ---------------------------------------
        #
        # Two independent failure modes share this loop, with separate budgets
        # because they are not the same problem:
        #   * the model call failed        -> transient retry, same request
        #   * the model replied with junk  -> repair, recompiled request
        repairs = 0
        model_retries = 0
        recompile = True
        """Enter VIEW on the first pass too: the step's own events must all
        follow its STEP_STARTED, so the phase is entered here rather than by
        the driver before the step index has advanced."""

        while True:
            if recompile:
                phase = await self._goto(run.id, phase, Phase.VIEW, state.step_index)
            recompile = True

            view = self.compiler.compile(
                step_id=step_id,
                state=state,
                tool_schemas=self.registry.schemas(run.task.tools),
                budget_note=json.dumps(self.policy.bundle.budget.remaining()),
            )
            await self._emit(
                EventType.CONTEXT_COMPILED,
                run.id,
                step_id=step_id,
                step_index=state.step_index,
                payload={
                    "tokens": view.token_estimate,
                    "budget": view.token_budget,
                    "included": view.included,
                    "dropped": view.dropped,
                    "snapshot": view.snapshot_hash,
                },
            )

            phase = await self._goto(run.id, phase, Phase.PROPOSE, state.step_index)

            try:
                # The fault hook lives inside the try so an injected provider
                # failure takes the same recorded path as a real one - an
                # injected fault that bypassed the logging would make the
                # benchmark measure the wrong thing.
                await self._fault("before_model", step=state.step_index)
                response = await self._call_model(run, state, step_id, view)
            except ForgeError as exc:
                # A transient provider failure is retried unchanged, per the
                # retry taxonomy in §9. Only an exhausted or non-transient
                # failure ends the step.
                if classify(exc) is RetryClass.TRANSIENT and self.retry.should_retry(
                    RetryClass.TRANSIENT, model_retries
                ):
                    model_retries += 1
                    delay = self.retry.delay_for(model_retries, self._rng)
                    await self._emit(
                        EventType.RETRY_SCHEDULED,
                        run.id,
                        step_id=step_id,
                        step_index=state.step_index,
                        payload={
                            "where": "PROPOSE",
                            "attempt": model_retries,
                            "delay_ms": delay,
                            "error": str(exc),
                        },
                    )
                    self.metrics.inc("forge_retries_total", where="propose")
                    await asyncio.sleep(delay / 1000)
                    continue

                await self._emit(
                    EventType.ERROR_RAISED,
                    run.id,
                    step_id=step_id,
                    step_index=state.step_index,
                    payload={"where": "PROPOSE", "error": str(exc),
                             "class": classify(exc).value},
                )
                state.failures.append(
                    {"step": state.step_index, "kind": "model_unavailable", "detail": str(exc)}
                )
                return phase, "failed"

            phase = await self._goto(run.id, phase, Phase.VALIDATE, state.step_index)
            try:
                proposal = self._parse_proposal(response.text, response.parsed)
            except ForgeError as exc:
                self.metrics.inc("forge_proposals_rejected_total")
                await self._emit(
                    EventType.PROPOSAL_REJECTED,
                    run.id,
                    step_id=step_id,
                    step_index=state.step_index,
                    payload={"error": str(exc), "repair": repairs},
                )
                state.failures.append(
                    {"step": state.step_index, "kind": "proposal_invalid", "detail": str(exc)}
                )
                if repairs >= self.config.max_repairs_per_step:
                    return phase, "failed"
                repairs += 1
                continue

            await self._emit(
                EventType.PROPOSAL_RECEIVED,
                run.id,
                step_id=step_id,
                step_index=state.step_index,
                payload={
                    "kind": proposal.kind.value,
                    "tool": proposal.tool,
                    "arguments": redact(proposal.arguments),
                    # The answer belongs here, not only on RUN_COMPLETED: the
                    # log must hold everything needed to rebuild the proposal,
                    # or replay cannot reconstruct an ANSWER turn and diverges
                    # on the final step of every run.
                    "answer": redact(proposal.answer),
                    "rationale_summary": proposal.rationale_summary,
                },
            )
            break

        assert proposal is not None  # the loop either breaks with one or returns

        # -- ANSWER short-circuits: nothing to authorize, nothing to dispatch.
        if proposal.kind is ProposalKind.ANSWER:
            phase = await self._goto(run.id, phase, Phase.EVALUATE, state.step_index)
            state.answer = proposal.answer
            await self._emit(
                EventType.EVALUATION_RECORDED,
                run.id,
                step_id=step_id,
                step_index=state.step_index,
                payload={"kind": "answer", "length": len(proposal.answer or "")},
            )
            return phase, "complete"

        if proposal.kind is not ProposalKind.TOOL_CALL:
            state.failures.append(
                {
                    "step": state.step_index,
                    "kind": "unsupported_proposal",
                    "detail": proposal.kind.value,
                }
            )
            phase = await self._goto(run.id, phase, Phase.EVALUATE, state.step_index)
            return await self._continue(run, state, phase)

        # -- AUTHORIZE ------------------------------------------------------
        phase = await self._goto(run.id, phase, Phase.AUTHORIZE, state.step_index)
        action = await self._authorize(run, state, step_id, proposal)
        if action is None:
            phase = await self._goto(run.id, phase, Phase.EVALUATE, state.step_index)
            return await self._continue(run, state, phase)

        signal = self.detector.record_action(
            self.registry.get(action.tool).fingerprint(action.arguments)
        )
        if signal:
            await self._emit(
                EventType.LOOP_DETECTED,
                run.id,
                step_id=step_id,
                step_index=state.step_index,
                payload={"kind": signal.kind, "detail": signal.detail},
            )
            raise LoopDetected(signal.detail, kind=signal.kind)

        # -- DISPATCH / OBSERVE / RECONCILE, with bounded retries ------------
        effect: Effect | None = None
        for attempt in range(1, self.config.max_dispatch_attempts + 1):
            phase = await self._goto(run.id, phase, Phase.DISPATCH, state.step_index)
            effect = await self._dispatch(run, state, step_id, action, attempt)

            phase = await self._goto(run.id, phase, Phase.OBSERVE, state.step_index)
            phase = await self._goto(run.id, phase, Phase.RECONCILE, state.step_index)

            verdict = reconcile(action, effect)
            await self._emit(
                EventType.EFFECT_RECONCILED,
                run.id,
                step_id=step_id,
                step_index=state.step_index,
                payload={"verdict": verdict.verdict.value, "reason": verdict.reason},
            )

            if verdict.verdict in (Verdict.MATCHED, Verdict.REUSED, Verdict.BENIGN_FAILURE):
                break

            if verdict.verdict is Verdict.NEEDS_COMPENSATION:
                await self._compensate(run, state, step_id, action)
                break

            if verdict.verdict is Verdict.RETRYABLE and self.retry.should_retry(
                RetryClass.TRANSIENT, attempt
            ):
                delay = self.retry.delay_for(attempt, self._rng)
                await self._emit(
                    EventType.RETRY_SCHEDULED,
                    run.id,
                    step_id=step_id,
                    step_index=state.step_index,
                    payload={"attempt": attempt, "delay_ms": delay, "tool": action.tool},
                )
                self.metrics.inc("forge_retries_total", tool=action.tool)
                await asyncio.sleep(delay / 1000)
                continue

            # MISMATCH, or retries exhausted.
            await self._emit(
                EventType.ERROR_RAISED,
                run.id,
                step_id=step_id,
                step_index=state.step_index,
                payload={"where": "RECONCILE", "error": verdict.reason},
            )
            return phase, "failed"

        assert effect is not None

        # -- COMMIT ---------------------------------------------------------
        phase = await self._goto(run.id, phase, Phase.COMMIT, state.step_index)
        self._apply_effect(state, action, effect)
        await self._emit(
            EventType.STEP_COMMITTED,
            run.id,
            step_id=step_id,
            step_index=state.step_index,
            payload={"tool": action.tool, "ok": effect.ok},
        )
        state.steps_committed += 1
        self.permits.expire_step(step_id)

        if state.step_index % self.config.checkpoint_every == 0:
            await self._checkpoint(run.id, state)

        # -- EVALUATE -------------------------------------------------------
        phase = await self._goto(run.id, phase, Phase.EVALUATE, state.step_index)
        await self._emit(
            EventType.EVALUATION_RECORDED,
            run.id,
            step_id=step_id,
            step_index=state.step_index,
            payload={
                "tool_ok": effect.ok,
                "latency_ms": effect.latency_ms,
                "observations": len(state.observations),
            },
        )

        progress = self.detector.record_step(len(state.observations))
        if progress:
            await self._emit(
                EventType.LOOP_DETECTED,
                run.id,
                step_id=step_id,
                step_index=state.step_index,
                payload={"kind": progress.kind, "detail": progress.detail},
            )
            raise LoopDetected(progress.detail, kind=progress.kind)

        return await self._continue(run, state, phase)

    async def _continue(self, run: Run, state: RunState, phase: Phase) -> tuple[Phase, str]:
        phase = await self._goto(run.id, phase, Phase.CONTINUE, state.step_index)
        return phase, "continue"

    # ------------------------------------------------------------- phases

    async def _call_model(
        self, run: Run, state: RunState, step_id: str, view: Any
    ) -> Any:
        attempts: list[RouteAttempt] = []
        self.gateway.on_attempt = attempts.append

        with self.tracer.span("model_call", step_id=step_id) as span:
            response = await self.gateway.complete(
                ModelRequest(
                    system=view.system,
                    messages=view.messages,
                    response_schema=PROPOSAL_SCHEMA,
                    tools=view.tool_schemas,
                    # A proposal is a small object, but a reasoning model
                    # spends output tokens thinking before it emits one. Too
                    # low a ceiling truncates that and returns nothing, which
                    # surfaces as "model output is not valid JSON" and looks
                    # like a model defect rather than a budget we set.
                    max_tokens=2048,
                )
            )
            span.set(provider=response.provider, model=response.model)

        state.usage = state.usage + response.usage
        self.policy.bundle.budget.tokens = state.usage.total_tokens
        self.policy.bundle.budget.usd = state.usage.usd
        self.metrics.inc("forge_model_calls_total", provider=response.provider)
        self.metrics.observe("forge_model_latency_ms", response.latency_ms)

        await self._emit(
            EventType.MODEL_CALLED,
            run.id,
            step_id=step_id,
            step_index=state.step_index,
            payload={
                "provider": response.provider,
                "model": response.model,
                "usage": response.usage.model_dump(),
                "latency_ms": response.latency_ms,
                "route": [
                    {"provider": a.provider, "ok": a.ok, "reason": a.reason} for a in attempts
                ],
                "context_snapshot": view.snapshot_hash,
            },
        )
        return response

    def _parse_proposal(self, text: str, parsed: dict[str, Any] | None) -> Proposal:
        """Turn raw model output into a validated `Proposal`, or reject it."""
        from forge.errors import ProposalInvalid

        payload = parsed
        if payload is None:
            try:
                candidate = json.loads(_strip_fences(text))
            except json.JSONDecodeError as exc:
                raise ProposalInvalid(
                    "model output is not valid JSON", detail=str(exc), head=text[:160]
                ) from exc
            if not isinstance(candidate, dict):
                raise ProposalInvalid("model output is not a JSON object", head=text[:160])
            payload = candidate

        try:
            return Proposal(
                kind=ProposalKind(str(payload.get("kind", "")).upper()),
                tool=payload.get("tool"),
                arguments=payload.get("arguments") or {},
                answer=payload.get("answer"),
                rationale_summary=payload.get("rationale_summary"),
                raw=payload,
            )
        except (ValidationError, ValueError) as exc:
            raise ProposalInvalid("proposal failed contract validation", detail=str(exc)) from exc

    async def _authorize(
        self, run: Run, state: RunState, step_id: str, proposal: Proposal
    ) -> Action | None:
        """Evaluate policy and mint a permit, or record a denial and move on."""
        tool_name = proposal.tool or ""

        if not self.registry.has(tool_name):
            decision_payload = {
                "decision": Decision.DENY.value,
                "reason": f"unknown tool {tool_name!r}",
                "capability": None,
                "policy_version": self.policy.version,
            }
            await self._emit(
                EventType.POLICY_DECIDED,
                run.id,
                step_id=step_id,
                step_index=state.step_index,
                payload=decision_payload,
            )
            state.denials.append(
                {"step": state.step_index, "capability": None, "reason": f"unknown tool {tool_name!r}"}
            )
            state.failures.append(
                {"step": state.step_index, "kind": "unknown_tool", "detail": tool_name}
            )
            self.metrics.inc("forge_policy_denials_total", reason="unknown_tool")
            return None

        spec = self.registry.get(tool_name)
        try:
            # An injected denial enters here, where real denials are decided,
            # so it produces an ordinary recorded DENY rather than an escaping
            # exception the run has no policy for.
            await self._fault("authorize", step=state.step_index, tool=tool_name)
            decision = self.policy.authorize_tool(
                spec=spec,
                arguments=proposal.arguments,
                task_allow_list=run.task.tools,
                invocations_used=self.permits.invocations(spec.capability),
            )
        except PolicyDenied as exc:
            decision = PolicyDecision(
                decision=Decision.DENY,
                reason=exc.reason or exc.message,
                policy_version=exc.policy_version or self.policy.version,
                capability=spec.capability,
                risk=spec.risk,
            )
        await self._emit(
            EventType.POLICY_DECIDED,
            run.id,
            step_id=step_id,
            step_index=state.step_index,
            payload={
                "decision": decision.decision.value,
                "reason": decision.reason,
                "capability": decision.capability,
                "risk": decision.risk.value,
                "tool": tool_name,
                "policy_version": decision.policy_version,
            },
        )

        if decision.decision is Decision.DENY:
            self.metrics.inc("forge_policy_denials_total", capability=decision.capability or "-")
            state.denials.append(
                {
                    "step": state.step_index,
                    "capability": decision.capability,
                    "reason": decision.reason,
                }
            )
            state.failures.append(
                {"step": state.step_index, "kind": "policy_denied", "tool": tool_name,
                 "detail": decision.reason}
            )
            return None

        action_hash = content_hash(tool_name, spec.version, proposal.arguments)
        key = idempotency_key(run.id, tool_name, proposal.arguments, attempt_group=0)

        if decision.decision is Decision.REQUIRE_APPROVAL:
            approved = await self._seek_approval(run, state, step_id, spec, proposal)
            if not approved:
                state.denials.append(
                    {
                        "step": state.step_index,
                        "capability": decision.capability,
                        "reason": "human approval refused",
                    }
                )
                state.failures.append(
                    {"step": state.step_index, "kind": "approval_refused", "tool": tool_name,
                     "detail": "operator declined"}
                )
                return None

        permit = self.permits.issue(
            run_id=run.id,
            step_id=step_id,
            capability=spec.capability,
            action_hash=action_hash,
            side_effect=spec.side_effect,
        )
        await self._emit(
            EventType.PERMIT_ISSUED,
            run.id,
            step_id=step_id,
            step_index=state.step_index,
            payload={
                "permit_id": permit.id,
                "capability": spec.capability,
                "side_effect": spec.side_effect.value,
            },
        )

        return Action(
            run_id=run.id,
            step_id=step_id,
            tool=tool_name,
            arguments=proposal.arguments,
            side_effect=spec.side_effect,
            idempotency_key=key,
            permit_id=permit.id,
            dry_run="dry_run" in decision.obligations,
            timeout_s=spec.timeout_s,
        )

    async def _seek_approval(
        self, run: Run, state: RunState, step_id: str, spec: Any, proposal: Proposal
    ) -> bool:
        await self._emit(
            EventType.APPROVAL_REQUESTED,
            run.id,
            step_id=step_id,
            step_index=state.step_index,
            payload={"tool": spec.name, "side_effect": spec.side_effect.value,
                     "arguments": redact(proposal.arguments)},
        )
        if self.approval is not None:
            approved = await self.approval(
                Action(
                    run_id=run.id, step_id=step_id, tool=spec.name,
                    arguments=proposal.arguments, side_effect=spec.side_effect,
                    idempotency_key="pending", permit_id="pending",
                )
            )
        else:
            approved = self.config.auto_approve

        await self._emit(
            EventType.APPROVAL_RESOLVED,
            run.id,
            step_id=step_id,
            step_index=state.step_index,
            payload={"approved": approved, "tool": spec.name},
        )
        return approved

    async def _dispatch(
        self, run: Run, state: RunState, step_id: str, action: Action, attempt: int
    ) -> Effect:
        """Execute an authorized action - or reuse the effect it already had.

        The idempotency check happens *before* the permit is redeemed and
        before the tool is touched, so a resumed run neither re-executes a
        write nor burns a fresh authorization for work already done.
        """
        # 1. Have we already produced this effect? (crash-resume path)
        recorded = await self.store.find_effect(run.id, action.idempotency_key)
        if recorded is not None:
            # Note: a reuse is NOT a duplicate. It is the mechanism that
            # prevents one. Only `append` reporting a dedupe counts below.
            await self._emit(
                EventType.EFFECT_REUSED,
                run.id,
                step_id=step_id,
                step_index=state.step_index,
                payload={**recorded.payload, "reused_from_seq": recorded.seq},
            )
            self.metrics.inc("forge_effects_reused_total", tool=action.tool)
            return Effect(
                action_id=action.id,
                idempotency_key=action.idempotency_key,
                ok=bool(recorded.payload.get("ok")),
                output=recorded.payload.get("output"),
                error=recorded.payload.get("error"),
                latency_ms=int(recorded.payload.get("latency_ms", 0)),
                evidence=recorded.payload.get("evidence") or {},
                reused=True,
            )

        # 2. Redeem the permit. A forged or mismatched permit stops us here.
        spec = self.registry.get(action.tool)
        self.permits.redeem(action.permit_id, action_hash=content_hash(
            action.tool, spec.version, action.arguments
        ))

        await self._emit(
            EventType.ACTION_DISPATCHED,
            run.id,
            step_id=step_id,
            step_index=state.step_index,
            payload={
                "tool": action.tool,
                "attempt": attempt,
                "side_effect": action.side_effect.value,
                "dry_run": action.dry_run,
                "fingerprint": spec.fingerprint(action.arguments),
                "arguments": redact(action.arguments),
            },
        )
        self.policy.bundle.budget.tool_calls += 1
        self.metrics.inc("forge_tool_calls_total", tool=action.tool)

        await self._fault("before_dispatch", step=state.step_index, action=action)

        # 3. Execute.
        with self.tracer.span("tool_call", tool=action.tool, attempt=attempt) as span:
            try:
                # Tool faults are injected *inside* this try so they travel the
                # same path a genuine tool failure does: classified, turned into
                # a recorded Effect, then reconciled. A fault that escaped here
                # would measure the harness, not the runtime.
                await self._fault("in_tool", step=state.step_index, action=action)
                outcome = await spec.invoke(action.arguments, dry_run=action.dry_run)
                ok, output, err = outcome.ok, outcome.output, outcome.error
                latency, evidence = outcome.latency_ms, outcome.evidence
                retry_class = None if ok else RetryClass.DETERMINISTIC
            except SimulatedCrash:
                raise
            except BaseException as exc:
                if isinstance(exc, KeyboardInterrupt | SystemExit):
                    raise
                retry_class = classify(exc)
                ok, output, err = False, None, f"{type(exc).__name__}: {exc}"
                latency, evidence = 0, getattr(exc, "context", {}) or {}
                span.set(error=err, retry_class=retry_class.value)

        await self._fault("after_dispatch", step=state.step_index, action=action)

        # 4. Record the effect. This append *is* the claim on the idempotency
        #    key - if it succeeds, the effect is durable; if the store says it
        #    already existed, another writer got there first and we reuse theirs.
        payload = {
            "tool": action.tool,
            "ok": ok,
            "output": redact(output),
            "error": err,
            "retry_class": retry_class.value if retry_class else None,
            "latency_ms": latency,
            "evidence": redact(evidence),
        }
        result = await self.store.append(
            NewEvent(
                type=EventType.EFFECT_OBSERVED,
                run_id=run.id,
                step_id=step_id,
                step_index=state.step_index,
                payload=payload,
                idempotency_key=action.idempotency_key,
            )
        )
        if result.deduplicated:
            self._duplicates += 1  # a genuine duplicate attempt: worth reporting
            self.metrics.inc("forge_duplicate_effects_total", tool=action.tool)

        self.metrics.observe("forge_tool_latency_ms", latency, tool=action.tool)
        if not ok:
            self.policy.bundle.budget.note_failure()
        else:
            self.policy.bundle.budget.note_success()

        return Effect(
            action_id=action.id,
            idempotency_key=action.idempotency_key,
            ok=ok,
            output=output,
            error=err,
            retry_class=retry_class,
            latency_ms=latency,
            evidence=evidence,
            reused=result.deduplicated,
        )

    async def _compensate(self, run: Run, state: RunState, step_id: str, action: Action) -> None:
        """Undo an effect that happened but should not stand (spec §8)."""
        spec = self.registry.get(action.tool)
        if spec.compensate is None:
            await self._emit(
                EventType.ERROR_RAISED,
                run.id,
                step_id=step_id,
                step_index=state.step_index,
                payload={"where": "COMPENSATE", "error": f"{action.tool} has no compensator"},
            )
            return
        try:
            await spec.compensate(**action.arguments)
            ok, detail = True, "compensated"
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"

        await self._emit(
            EventType.COMPENSATION_APPLIED,
            run.id,
            step_id=step_id,
            step_index=state.step_index,
            payload={"tool": action.tool, "ok": ok, "detail": detail},
        )
        self.metrics.inc("forge_compensations_total", tool=action.tool, ok=str(ok))

    def _apply_effect(self, state: RunState, action: Action, effect: Effect) -> None:
        """Fold one effect into in-memory state, mirroring `project()`."""
        state.completed_effects[effect.idempotency_key] = {
            "tool": action.tool,
            "ok": effect.ok,
            "output": effect.output,
        }
        if effect.ok:
            state.observations.append(
                {"step": state.step_index, "tool": action.tool, "output": effect.output}
            )
        else:
            state.failures.append(
                {
                    "step": state.step_index,
                    "kind": "tool_failed",
                    "tool": action.tool,
                    "detail": effect.error or "",
                }
            )

    async def _checkpoint(self, run_id: str, state: RunState) -> None:
        """Atomically snapshot resumable state (spec §9)."""
        state.last_seq = self._last_seq
        checkpoint = Checkpoint(
            run_id=run_id,
            step_index=state.step_index,
            last_seq=state.last_seq,
            state=state.to_dict(),
            context_digest=content_hash(state.observations, state.failures)[:16],
            created_at=datetime.now(UTC),
        )
        await self.store.write_checkpoint(checkpoint)
        await self._emit(
            EventType.CHECKPOINT_WRITTEN,
            run_id,
            step_index=state.step_index,
            payload={
                "checkpoint_id": checkpoint.id,
                "last_seq": checkpoint.last_seq,
                "effects_known": len(state.completed_effects),
            },
        )
        self.metrics.inc("forge_checkpoints_total")

    # ------------------------------------------------------------ plumbing

    async def _goto(self, run_id: str, current: Phase, nxt: Phase, step_index: int) -> Phase:
        """Move phases. Illegal edges raise before anything else happens."""
        assert_transition(current, nxt)
        await self._emit(
            EventType.PHASE_ENTERED,
            run_id,
            step_index=step_index,
            payload={"phase": nxt.value, "from": current.value},
        )
        await self._fault("phase", step=step_index, phase=nxt)
        return nxt

    async def _emit(
        self,
        type_: EventType,
        run_id: str,
        *,
        step_id: str | None = None,
        step_index: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Append an event and advance the local sequence watermark.

        The watermark is what a checkpoint records, so it must track every
        append - otherwise checkpoints all claim seq 0 and resume degrades
        from "replay the tail" to "replay everything".
        """
        result = await self.store.append(
            NewEvent(
                type=type_,
                run_id=run_id,
                step_id=step_id,
                step_index=step_index,
                payload=payload or {},
            )
        )
        self._last_seq = max(self._last_seq, result.event.seq)
        return result.event.seq

    async def _fault(self, hook: str, **ctx: Any) -> None:
        if self.faults is not None:
            await self.faults.check(hook, **ctx)


def _strip_fences(text: str) -> str:
    """Small models wrap JSON in markdown fences. Tolerate that, quietly."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1] if "\n" in stripped else stripped
        if stripped.endswith("```"):
            stripped = stripped[: stripped.rfind("```")]
    # Some models emit a preamble before the object; take the outermost braces.
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start : end + 1]
    return stripped
