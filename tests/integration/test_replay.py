"""Replay determinism and version diffing - spec §19, §27.6."""

from __future__ import annotations

from forge.core.contracts import TaskSpec
from forge.core.enums import EventType, RunStatus
from forge.evaluation.replay import (
    compare,
    replay_run,
    script_from_trajectory,
    trajectory_of,
)
from forge.state.sqlite_store import SQLiteEventStore
from tests.conftest import lookup_script, run

TOOLS = ["search_corpus", "read_document", "calculate", "save_note"]


def test_proposal_event_records_everything_needed_to_rebuild_it(make_runtime) -> None:
    """Regression: an ANSWER proposal must be reconstructable from the log.

    The answer was originally recorded only on RUN_COMPLETED, so replay
    rebuilt an ANSWER turn with no answer, failed contract validation, and
    diverged on the final step of every run. The log has to be sufficient.
    """
    runtime = make_runtime(lookup_script())

    async def main():
        await runtime.store.open()
        result = await runtime.start(TaskSpec(goal="g", tools=TOOLS))
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return events

    events = run(main())
    proposals = [e for e in events if e.type is EventType.PROPOSAL_RECEIVED]
    answers = [e for e in proposals if e.payload["kind"] == "ANSWER"]
    assert answers, "the run should have ended with an ANSWER proposal"
    assert answers[0].payload.get("answer"), "the answer must be in the proposal event"

    # And the reconstructed script must be independently valid.
    rebuilt = script_from_trajectory(events)
    last = rebuilt._turns[-1].proposal
    assert last["kind"] == "ANSWER" and last.get("answer")


def test_replay_of_a_run_is_identical(make_runtime, store_path) -> None:
    """A recorded run, re-executed against its own proposals, must not drift."""

    async def main():
        store = SQLiteEventStore(store_path)
        await store.open()
        original = make_runtime(lookup_script(), store=store)
        result = await original.start(TaskSpec(goal="replay me", tools=TOOLS))

        diff = await replay_run(
            result.run_id,
            store=store,
            build_runtime=lambda provider: make_runtime(provider, store=store),
        )
        await store.close()
        return result, diff

    result, diff = run(main())
    assert result.status is RunStatus.COMPLETED
    assert diff.identical, diff.render()
    assert diff.left_len > 1


def test_trajectory_ignores_incidental_differences() -> None:
    """Latency and ids differ between runs without being behavioural changes."""
    from forge.evaluation.replay import TrajectoryStep

    a = TrajectoryStep(index=1, proposal_kind="TOOL_CALL", tool="t",
                       arguments={"x": 1}, decision="ALLOW", effect_ok=True)
    b = TrajectoryStep(index=1, proposal_kind="TOOL_CALL", tool="t",
                       arguments={"x": 1}, decision="ALLOW", effect_ok=True)
    assert compare([a], [b]).identical


def test_diff_names_the_first_divergent_step() -> None:
    from forge.evaluation.replay import TrajectoryStep

    baseline = [
        TrajectoryStep(index=1, proposal_kind="TOOL_CALL", tool="search_corpus"),
        TrajectoryStep(index=2, proposal_kind="TOOL_CALL", tool="read_document"),
    ]
    changed = [
        TrajectoryStep(index=1, proposal_kind="TOOL_CALL", tool="search_corpus"),
        TrajectoryStep(index=2, proposal_kind="TOOL_CALL", tool="calculate"),
    ]
    diff = compare(baseline, changed)
    assert not diff.identical
    assert diff.first_divergence == 2
    assert "read_document" in diff.left and "calculate" in diff.right


def test_diff_reports_length_mismatch() -> None:
    from forge.evaluation.replay import TrajectoryStep

    short = [TrajectoryStep(index=1, proposal_kind="ANSWER")]
    long = [
        TrajectoryStep(index=1, proposal_kind="ANSWER"),
        TrajectoryStep(index=2, proposal_kind="ANSWER"),
    ]
    diff = compare(short, long)
    assert not diff.identical
    assert "length differs" in diff.render()


def test_trajectory_captures_the_decision_chain(make_runtime) -> None:
    runtime = make_runtime(lookup_script())

    async def main():
        await runtime.store.open()
        result = await runtime.start(TaskSpec(goal="g", tools=TOOLS))
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return events

    steps = trajectory_of(run(main()))
    tool_steps = [s for s in steps if s.tool]
    assert tool_steps
    assert all(s.decision == "ALLOW" for s in tool_steps)
    assert all(s.verdict == "MATCHED" for s in tool_steps)
