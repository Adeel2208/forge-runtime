"""End-to-end runtime behaviour against a real event store."""

from __future__ import annotations

from forge.core.contracts import TaskSpec
from forge.core.enums import EventType, RunStatus
from forge.evaluation.faults import FaultClass, FaultInjector
from forge.llm.mock import MockProvider, ScriptedTurn
from forge.runtime.loop import RuntimeConfig
from tests.conftest import answer_script, lookup_script, run, write_script


def _spec(**kw) -> TaskSpec:
    defaults = dict(goal="test goal", tools=["search_corpus", "read_document", "calculate",
                                             "save_note", "flaky_lookup", "publish"])
    defaults.update(kw)
    return TaskSpec(**defaults)  # type: ignore[arg-type]


def test_answer_only_run_completes(make_runtime) -> None:
    runtime = make_runtime(answer_script("42"))

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        await runtime.store.close()
        return result

    result = run(main())
    assert result.status is RunStatus.COMPLETED
    assert result.answer == "42"
    assert result.usage.usd == 0.0


def test_tool_run_completes_and_logs_every_phase(make_runtime) -> None:
    runtime = make_runtime(lookup_script())

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return result, events

    result, events = run(main())
    assert result.status is RunStatus.COMPLETED
    kinds = {e.type for e in events}

    # The full lifecycle is present in the log, not just the outcome.
    for required in (
        EventType.RUN_CREATED, EventType.CONTEXT_COMPILED, EventType.MODEL_CALLED,
        EventType.PROPOSAL_RECEIVED, EventType.POLICY_DECIDED, EventType.PERMIT_ISSUED,
        EventType.ACTION_DISPATCHED, EventType.EFFECT_OBSERVED, EventType.EFFECT_RECONCILED,
        EventType.STEP_COMMITTED, EventType.CHECKPOINT_WRITTEN, EventType.RUN_COMPLETED,
    ):
        assert required in kinds, f"missing {required} in the audit trail"


def test_every_dispatch_is_preceded_by_a_policy_decision(make_runtime) -> None:
    """Spec §27.2, checked against the log rather than the code."""
    runtime = make_runtime(lookup_script())

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return events

    events = run(main())
    decisions = 0
    for ev in events:
        if ev.type is EventType.POLICY_DECIDED:
            decisions += 1
        elif ev.type is EventType.ACTION_DISPATCHED:
            assert decisions > 0, "a dispatch occurred with no prior policy decision"


def test_reversible_write_is_applied(make_runtime) -> None:
    from forge.tools.builtin import WORKSPACE

    runtime = make_runtime(write_script())

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        await runtime.store.close()
        return result

    result = run(main())
    assert result.status is RunStatus.COMPLETED
    assert WORKSPACE.get("answer") == "76"


def test_malformed_output_is_repaired_not_fatal(make_runtime) -> None:
    """A truncated JSON reply costs a reprompt, not the run."""
    script = [ScriptedTurn(proposal={"kind": "ANSWER", "answer": "recovered fine"},
                           malformed=True),
              ScriptedTurn(proposal={"kind": "ANSWER", "answer": "recovered fine"})]
    runtime = make_runtime(MockProvider(script))

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return result, events

    result, events = run(main())
    assert result.status is RunStatus.COMPLETED
    assert result.answer == "recovered fine"
    assert any(e.type is EventType.PROPOSAL_REJECTED for e in events)


def test_transient_tool_failure_is_retried(make_runtime) -> None:
    from forge.tools.builtin import set_flakiness

    set_flakiness(1)  # fail once, then succeed
    script = [
        {"proposal": {"kind": "TOOL_CALL", "tool": "flaky_lookup",
                      "arguments": {"query": "x"}}},
        {"proposal": {"kind": "ANSWER", "answer": "eventually worked"}},
    ]
    runtime = make_runtime(script)

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return result, events

    result, events = run(main())
    assert result.status is RunStatus.COMPLETED
    assert any(e.type is EventType.RETRY_SCHEDULED for e in events)


def test_unknown_tool_is_denied_and_the_run_continues(make_runtime) -> None:
    script = [
        {"proposal": {"kind": "TOOL_CALL", "tool": "definitely_not_a_tool", "arguments": {}}},
        {"proposal": {"kind": "ANSWER", "answer": "moved on"}},
    ]
    runtime = make_runtime(script)

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        await runtime.store.close()
        return result

    result = run(main())
    assert result.status is RunStatus.COMPLETED
    assert result.denials and "unknown tool" in result.denials[0]["reason"]


def test_action_loop_is_bounded(make_runtime) -> None:
    """A model stuck on one call is stopped by counting, not persuasion."""
    script = [ScriptedTurn(
        proposal={"kind": "TOOL_CALL", "tool": "search_corpus", "arguments": {"query": "x"}},
        repeat=10,
    )]
    runtime = make_runtime(MockProvider(script))

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return result, events

    result, events = run(main())
    assert result.status is RunStatus.FAILED
    assert any(e.type is EventType.LOOP_DETECTED for e in events)
    assert result.steps < 10, "the loop must be cut short"


def test_step_budget_terminates_a_runaway_run(make_runtime) -> None:
    script = [ScriptedTurn(
        proposal={"kind": "TOOL_CALL", "tool": "calculate", "arguments": {"expression": "1+1"}},
        repeat=50,
    )]
    runtime = make_runtime(
        MockProvider(script), config=RuntimeConfig(max_steps=4)
    )
    runtime.detector.max_identical = 999
    runtime.detector.max_steps_without_progress = 999

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec(max_steps=4))
        await runtime.store.close()
        return result

    result = run(main())
    assert result.status is RunStatus.FAILED
    assert result.steps <= 4


def test_transient_llm_timeout_is_retried_not_fatal(make_runtime) -> None:
    """A transient provider failure costs a backoff, not the run (spec §9)."""
    runtime = make_runtime(
        answer_script("survived"),
        faults=FaultInjector.single(FaultClass.LLM_TIMEOUT, at_step=1),
    )

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return result, events

    result, events = run(main())
    assert result.status is RunStatus.COMPLETED
    assert result.answer == "survived"
    retries = [e for e in events if e.type is EventType.RETRY_SCHEDULED]
    assert retries and retries[0].payload["where"] == "PROPOSE"


def test_unrecoverable_llm_failure_ends_the_run(make_runtime) -> None:
    """Retries are bounded: a provider that never recovers fails the run."""
    from forge.evaluation.faults import FaultSpec

    runtime = make_runtime(
        answer_script(),
        faults=FaultInjector(
            specs=[FaultSpec(kind=FaultClass.LLM_TIMEOUT, at_step=None, max_fires=99)]
        ),
    )

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return result, events

    result, events = run(main())
    assert result.status is RunStatus.FAILED
    assert any(e.type is EventType.ERROR_RAISED for e in events)


def test_injected_tool_timeout_takes_the_real_failure_path(make_runtime) -> None:
    """A tool fault must become a recorded Effect, not an escaping exception.

    Injected at the wrong boundary it would skip effect recording and
    reconciliation, and the benchmark would measure the harness rather than
    the runtime. This pins the placement.
    """
    runtime = make_runtime(
        lookup_script(), faults=FaultInjector.single(FaultClass.TOOL_TIMEOUT, at_step=1)
    )

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return result, events

    result, events = run(main())
    assert result.status is RunStatus.COMPLETED, "a transient tool failure is survivable"
    failed = [e for e in events if e.type is EventType.EFFECT_OBSERVED
              and not e.payload.get("ok")]
    assert failed, "the injected failure must be recorded as an effect"
    assert failed[0].payload["retry_class"] == "TRANSIENT"
    assert any(e.type is EventType.RETRY_SCHEDULED for e in events)


def test_injected_policy_denial_is_recorded_as_a_denial(make_runtime) -> None:
    """An injected denial enters at AUTHORIZE and behaves like a real one."""
    runtime = make_runtime(
        lookup_script(), faults=FaultInjector.single(FaultClass.POLICY_DENIAL, at_step=1)
    )

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return result, events

    result, events = run(main())
    assert result.denials, "the injected denial must be recorded"
    decided = [e for e in events if e.type is EventType.POLICY_DECIDED]
    assert any(e.payload["decision"] == "DENY" for e in decided)
    assert result.status is RunStatus.COMPLETED, "a denial is survivable, not fatal"


def test_phase_events_are_attributed_to_their_own_step(make_runtime) -> None:
    """Regression: a step's events must all follow its STEP_STARTED.

    The VIEW transition for step N+1 was previously entered by the driver
    before the step index advanced, so every step's first phase event was
    logged against the *previous* step - quietly corrupting the audit trail
    and any per-step analysis built on it.
    """
    runtime = make_runtime(lookup_script())

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return events

    events = run(main())
    current: int | None = None
    for ev in events:
        if ev.type is EventType.STEP_STARTED:
            current = ev.step_index
        elif ev.type is EventType.PHASE_ENTERED and ev.payload["phase"] == "VIEW":
            assert ev.step_index == current, (
                f"VIEW for step {ev.step_index} logged while step {current} was current"
            )

    # And every step index that appears has a STEP_STARTED introducing it.
    started = {e.step_index for e in events if e.type is EventType.STEP_STARTED}
    phased = {e.step_index for e in events
              if e.type is EventType.PHASE_ENTERED and e.step_index}
    assert phased <= started, f"phases logged for steps never started: {phased - started}"


def test_telemetry_spans_cover_the_run(make_runtime) -> None:
    runtime = make_runtime(lookup_script())

    async def main():
        await runtime.store.open()
        await runtime.start(_spec())
        await runtime.store.close()

    run(main())
    names = {s.name for s in runtime.tracer.spans}
    assert {"run", "step", "model_call", "tool_call"} <= names
    assert all(s.trace_id == runtime.tracer.trace_id for s in runtime.tracer.spans)


def test_metrics_are_recorded(make_runtime) -> None:
    runtime = make_runtime(lookup_script())

    async def main():
        await runtime.store.open()
        await runtime.start(_spec())
        await runtime.store.close()

    run(main())
    assert runtime.metrics.get("forge_runs_total", status="COMPLETED") == 1
    assert runtime.metrics.get("forge_tool_calls_total", tool="search_corpus") == 1
    assert "forge_tool_calls_total" in runtime.metrics.render()
