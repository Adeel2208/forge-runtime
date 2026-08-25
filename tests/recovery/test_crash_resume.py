"""Crash and resume correctness - spec §27.1.

The gate this suite defends:

    A worker can crash and a run can resume correctly from a checkpoint,
    with **zero duplicated external effects**.

`test_hard_process_kill_then_resume` is the one that actually settles it: it
kills a real OS process with `os._exit`, so nothing in the runtime gets a
chance to tidy up, and then resumes from whatever reached the disk.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from forge.core.contracts import TaskSpec
from forge.core.enums import EventType, RunStatus
from forge.evaluation.faults import FaultClass, FaultInjector
from forge.runtime.loop import SimulatedCrash
from forge.state.sqlite_store import SQLiteEventStore
from tests.conftest import run

pytestmark = pytest.mark.recovery

CRASH_SCRIPT = [
    {"proposal": {"kind": "TOOL_CALL", "tool": "calculate",
                  "arguments": {"expression": "10 + 5"}}},
    {"proposal": {"kind": "TOOL_CALL", "tool": "save_note",
                  "arguments": {"name": "step2", "content": "fifteen"}}},
    {"proposal": {"kind": "TOOL_CALL", "tool": "save_note",
                  "arguments": {"name": "step3", "content": "final"}}},
    {"proposal": {"kind": "ANSWER", "answer": "15, saved."}},
]

TOOLS = ["calculate", "save_note"]


def _effect_events(events) -> tuple[list, list]:
    performed = [e for e in events if e.type is EventType.EFFECT_OBSERVED]
    reused = [e for e in events if e.type is EventType.EFFECT_REUSED]
    return performed, reused


def test_crash_then_resume_completes_without_duplicate_effects(make_runtime, store_path) -> None:
    async def main():
        store = SQLiteEventStore(store_path)
        await store.open()

        crashing = make_runtime(
            CRASH_SCRIPT, store=store,
            faults=FaultInjector.single(FaultClass.WORKER_CRASH, at_step=3),
        )
        spec = TaskSpec(goal="crash test", tools=TOOLS, max_steps=8)
        with pytest.raises(SimulatedCrash):
            await crashing.start(spec)

        runs = await store.list_runs(limit=1)
        run_id = str(runs[0]["run_id"])

        # A brand-new runtime object: nothing carries over in memory.
        fresh = make_runtime(CRASH_SCRIPT, store=store)
        result = await fresh.resume(run_id)
        events = await store.read(run_id)
        await store.close()
        return result, events

    result, events = run(main())
    performed, reused = _effect_events(events)
    keys = [e.idempotency_key for e in performed]

    assert result.status is RunStatus.COMPLETED
    assert result.resumed is True
    assert result.duplicate_effects == 0, "resume must not duplicate an external effect"
    assert len(keys) == len(set(keys)), f"duplicate idempotency keys: {keys}"
    assert reused, "resume should have reused at least one already-performed effect"


def test_resume_restores_state_from_the_checkpoint(make_runtime, store_path) -> None:
    async def main():
        store = SQLiteEventStore(store_path)
        await store.open()
        crashing = make_runtime(
            CRASH_SCRIPT, store=store,
            faults=FaultInjector.single(FaultClass.WORKER_CRASH, at_step=3),
        )
        with pytest.raises(SimulatedCrash):
            await crashing.start(TaskSpec(goal="crash test", tools=TOOLS, max_steps=8))
        runs = await store.list_runs(limit=1)
        run_id = str(runs[0]["run_id"])
        checkpoint = await store.latest_checkpoint(run_id)
        await store.close()
        return checkpoint

    checkpoint = run(main())
    assert checkpoint is not None
    assert checkpoint.step_index == 2, "two steps committed before the step-3 crash"
    assert checkpoint.last_seq > 0, "the checkpoint watermark must be real"
    assert checkpoint.state["completed_effects"], "effects must survive in the checkpoint"


def test_crash_writes_no_epilogue(make_runtime, store_path) -> None:
    """A killed worker must not get to write RUN_FAILED - it is not dead-dead."""

    async def main():
        store = SQLiteEventStore(store_path)
        await store.open()
        crashing = make_runtime(
            CRASH_SCRIPT, store=store,
            faults=FaultInjector.single(FaultClass.WORKER_CRASH, at_step=2),
        )
        with pytest.raises(SimulatedCrash):
            await crashing.start(TaskSpec(goal="crash", tools=TOOLS, max_steps=8))
        runs = await store.list_runs(limit=1)
        events = await store.read(str(runs[0]["run_id"]))
        await store.close()
        return events

    events = run(main())
    assert not any(e.type is EventType.RUN_FAILED for e in events)
    assert not any(e.type is EventType.RUN_COMPLETED for e in events)


def test_repeated_crashes_still_converge(make_runtime, store_path) -> None:
    """Crash at step 2, resume, crash at step 3, resume again - still exactly-once."""

    async def main():
        store = SQLiteEventStore(store_path)
        await store.open()
        spec = TaskSpec(goal="crash twice", tools=TOOLS, max_steps=10)

        first = make_runtime(
            CRASH_SCRIPT, store=store,
            faults=FaultInjector.single(FaultClass.WORKER_CRASH, at_step=2),
        )
        with pytest.raises(SimulatedCrash):
            await first.start(spec)
        run_id = str((await store.list_runs(limit=1))[0]["run_id"])

        # at_step=None: crash at the next *real* dispatch. Step indices shift
        # after a resume, and reused effects skip the dispatch path entirely,
        # so pinning an index here would quietly stop crashing.
        second = make_runtime(
            CRASH_SCRIPT, store=store,
            faults=FaultInjector.single(FaultClass.WORKER_CRASH, at_step=None),
        )
        with pytest.raises(SimulatedCrash):
            await second.resume(run_id)

        third = make_runtime(CRASH_SCRIPT, store=store)
        result = await third.resume(run_id)
        events = await store.read(run_id)
        await store.close()
        return result, events

    result, events = run(main())
    performed, _ = _effect_events(events)
    keys = [e.idempotency_key for e in performed]
    assert result.status is RunStatus.COMPLETED
    assert result.duplicate_effects == 0
    assert len(keys) == len(set(keys)), "two crashes must still yield one effect per key"


def test_resume_of_a_finished_run_is_a_no_op(make_runtime, store_path) -> None:
    async def main():
        store = SQLiteEventStore(store_path)
        await store.open()
        runtime = make_runtime(CRASH_SCRIPT, store=store)
        first = await runtime.start(TaskSpec(goal="finish", tools=TOOLS, max_steps=8))
        again = make_runtime(CRASH_SCRIPT, store=store)
        second = await again.resume(first.run_id)
        events = await store.read(first.run_id)
        await store.close()
        return first, second, events

    first, second, events = run(main())
    performed, _ = _effect_events(events)
    assert first.status is RunStatus.COMPLETED
    assert second.status is RunStatus.COMPLETED
    assert second.steps == first.steps, "resuming a finished run must not redo work"
    keys = [e.idempotency_key for e in performed]
    assert len(keys) == len(set(keys))


def test_hard_process_kill_then_resume(store_path: Path) -> None:
    """The real thing: kill an OS process with `os._exit`, then resume.

    No exception handling, no cleanup, no flush - whatever survives is what
    SQLite actually put on disk. This is the test that makes §27.1 a fact
    rather than a claim.
    """
    run_id = "run_hardkill01"
    worker = subprocess.run(
        [sys.executable, "-m", "tests.recovery._crash_worker", str(store_path), run_id, "3"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert worker.returncode == 137, (
        f"worker should have died hard; got {worker.returncode}\n{worker.stderr}"
    )

    async def main():
        from tests.recovery._crash_worker import build

        store = SQLiteEventStore(store_path)
        await store.open()
        result = await build(store).resume(run_id)
        events = await store.read(run_id)
        await store.close()
        return result, events

    result, events = run(main())
    performed, reused = _effect_events(events)
    keys = [e.idempotency_key for e in performed]

    assert result.status is RunStatus.COMPLETED, f"resume failed: {result.error}"
    assert result.duplicate_effects == 0
    assert len(keys) == len(set(keys)), f"the killed process left duplicates: {keys}"
    assert reused, "the resumed run should reuse effects the dead worker completed"
