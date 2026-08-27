"""Helping a model stop.

Measured against five local Ollama models, every failure had the same shape:
the model completed the actual work and then could not recognise it was done,
repeating a finished call until the loop bound killed the run. Three of the
four had finished two steps before they were stopped.

None of that is a capability ceiling. A model that is never told it repeated
itself cannot correct, and a prompt that ends with "propose one operation"
spends its most valuable position asking for another tool call. These tests
cover the three fixes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from forge.context.compiler import ContextCompiler
from forge.core.enums import EventType, RunStatus
from forge.core.events import Event
from forge.llm.mock import MockProvider, ScriptedTurn
from forge.state.projection import RunState, project
from tests.conftest import run


def _spec():
    from forge.core.contracts import TaskSpec

    return TaskSpec(
        goal="save a note then search for it",
        tools=["search_corpus", "read_document", "calculate", "save_note"],
        max_steps=12,
    )


# -- 1. a suppressed duplicate is visibly a duplicate ------------------------


def test_a_reused_effect_is_marked_in_state() -> None:
    events = [
        Event(seq=1, ts=datetime.now(UTC), type=EventType.EFFECT_OBSERVED, run_id="r",
              step_index=1, idempotency_key="k", payload={"ok": True, "tool": "t", "output": "o"}),
        Event(seq=2, ts=datetime.now(UTC), type=EventType.EFFECT_REUSED, run_id="r",
              step_index=2, idempotency_key="k", payload={"ok": True, "tool": "t", "output": "o"}),
    ]
    state = project(events)
    assert [o["reused"] for o in state.observations] == [False, True]


def test_the_compiler_labels_a_repeated_call() -> None:
    """Otherwise a suppressed duplicate reads exactly like a fresh success."""
    state = RunState(run_id="r", goal="g")
    state.observations = [
        {"step": 1, "tool": "save_note", "output": "saved", "reused": False},
        {"step": 2, "tool": "save_note", "output": "saved", "reused": True},
    ]
    view = ContextCompiler().compile(step_id="s", state=state, tool_schemas=[])
    body = view.messages[0]["content"]

    assert body.count("ALREADY DONE - you repeated this") == 1
    assert "step 2 save_note [ALREADY DONE" in body


# -- 2. the closing instruction asks the live question -----------------------


def test_the_closing_instruction_leads_with_answer_once_work_exists() -> None:
    state = RunState(run_id="r", goal="g")
    state.observations = [
        {"step": 1, "tool": "save_note", "output": "saved", "reused": False},
        {"step": 2, "tool": "search_notes", "output": "found", "reused": False},
    ]
    body = ContextCompiler().compile(
        step_id="s", state=state, tool_schemas=[]
    ).messages[0]["content"]

    assert "NOW - DECIDE" in body
    assert "You have already run: save_note, search_notes" in body
    # The instruction must make the model CHECK the goal, not bias it toward
    # answering. An earlier wording ended "this is usually the right move",
    # and it cost correctness: models answered after doing half the task, and
    # one reported a search result for a search it never ran. A run that
    # fails honestly beats a run that completes and lies.
    assert "may ask for more than one thing" in body
    assert "usually the right move" not in body
    assert "still not done" in body


def test_the_system_prompt_forbids_reporting_unobserved_results() -> None:
    """The failure mode this exists to stop: a model that answers with the
    result of a tool it never called."""
    prompt = ContextCompiler.SYSTEM_PROMPT
    assert "Never state a result you did not observe" in prompt
    assert "every part is done" in prompt


def test_the_first_step_still_just_asks_for_an_operation() -> None:
    """With nothing done yet there is no decision to make."""
    body = ContextCompiler().compile(
        step_id="s", state=RunState(run_id="r", goal="g"), tool_schemas=[]
    ).messages[0]["content"]

    assert "NOW - DECIDE" not in body
    assert "Propose exactly one next operation" in body


def test_the_done_list_is_deduplicated_and_ordered() -> None:
    state = RunState(run_id="r", goal="g")
    state.observations = [
        {"step": 1, "tool": "a", "output": "x", "reused": False},
        {"step": 2, "tool": "b", "output": "x", "reused": False},
        {"step": 3, "tool": "a", "output": "x", "reused": True},
    ]
    body = ContextCompiler().compile(
        step_id="s", state=state, tool_schemas=[]
    ).messages[0]["content"]
    assert "You have already run: a, b." in body


# -- 3. warn once, then halt -------------------------------------------------


def test_a_repeating_model_is_warned_before_it_is_killed(make_runtime) -> None:
    """The bound is not weakened; the model gets exactly one chance to stop."""
    script = [ScriptedTurn(
        proposal={"kind": "TOOL_CALL", "tool": "search_corpus", "arguments": {"query": "x"}},
        repeat=12,
    )]
    runtime = make_runtime(MockProvider(script))

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return result, events

    result, events = run(main())
    loops = [e for e in events if e.type is EventType.LOOP_DETECTED]

    assert [e.payload["action"] for e in loops] == ["warned", "halted"]
    assert result.status is RunStatus.FAILED, "a model that ignores the warning still halts"

    # The warning must reach the model as something it will actually read.
    rejections = [
        e for e in events
        if e.type is EventType.PROPOSAL_REJECTED and "already used" in str(e.payload.get("error"))
    ]
    assert len(rejections) == 1
    assert "ANSWER" in str(rejections[0].payload["error"])


def test_a_model_that_takes_the_hint_completes(make_runtime) -> None:
    """The whole point: the warning turns a failed run into a finished one."""
    # Three identical reads trips the bound on the third, which is warned
    # rather than fatal; the model then answers on the next turn. A fourth
    # repeat would halt, which is the behaviour the previous test covers.
    script = [
        ScriptedTurn(proposal={"kind": "TOOL_CALL", "tool": "search_corpus",
                               "arguments": {"query": "x"}}, repeat=3),
        ScriptedTurn(proposal={"kind": "ANSWER", "answer": "found it"}),
    ]
    runtime = make_runtime(MockProvider(script))

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        await runtime.store.close()
        return result

    result = run(main())
    assert result.status is RunStatus.COMPLETED
    assert result.answer == "found it"


def test_the_warning_does_not_dispatch_the_repeated_call(make_runtime) -> None:
    """A warned step must not also perform the effect it warned about."""
    script = [
        ScriptedTurn(proposal={"kind": "TOOL_CALL", "tool": "save_note",
                               "arguments": {"name": "n", "content": "c"}}, repeat=3),
        ScriptedTurn(proposal={"kind": "ANSWER", "answer": "done"}),
    ]
    runtime = make_runtime(MockProvider(script))

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return result, events

    _result, events = run(main())
    warned_steps = {
        e.step_index for e in events
        if e.type is EventType.LOOP_DETECTED and e.payload.get("action") == "warned"
    }
    dispatched_steps = {
        e.step_index for e in events if e.type is EventType.ACTION_DISPATCHED
    }
    assert warned_steps.isdisjoint(dispatched_steps)
