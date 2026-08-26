"""Event-log retention.

An append-only log grows forever unless something removes from it. Pruning is
the one operation that deletes history, so its rules matter more than its
implementation: whole runs only, finished runs only by default, and nothing
that could still be recovered.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from forge.core.enums import EventType
from forge.core.events import NewEvent
from forge.state.sqlite_store import SQLiteEventStore
from tests.conftest import run


async def _seed(store: SQLiteEventStore, run_id: str, *, finished: bool, age_days: float) -> None:
    """Write a run, then backdate it so retention has something to act on."""
    await store.append(NewEvent(type=EventType.RUN_CREATED, run_id=run_id, payload={"goal": "g"}))
    await store.append(
        NewEvent(type=EventType.STEP_COMMITTED, run_id=run_id, step_index=1, payload={})
    )
    if finished:
        await store.append(
            NewEvent(type=EventType.RUN_COMPLETED, run_id=run_id, payload={"answer": "a"})
        )
    old = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    conn = store._require()
    conn.execute("UPDATE events SET ts=? WHERE run_id=?", (old, run_id))


def test_prune_removes_only_old_finished_runs(store_path) -> None:
    async def main():
        store = SQLiteEventStore(store_path)
        await store.open()
        await _seed(store, "run_old_done", finished=True, age_days=90)
        await _seed(store, "run_new_done", finished=True, age_days=1)
        await _seed(store, "run_old_open", finished=False, age_days=90)

        removed = await store.prune(older_than_days=30)
        survivors = {
            r for r in ("run_old_done", "run_new_done", "run_old_open")
            if await store.read(r)
        }
        await store.close()
        return removed, survivors

    removed, survivors = run(main())
    assert removed == 1
    assert survivors == {"run_new_done", "run_old_open"}


def test_prune_can_include_unfinished_runs_when_asked(store_path) -> None:
    """Explicit opt-in: abandoned runs are recoverable until you say otherwise."""

    async def main():
        store = SQLiteEventStore(store_path)
        await store.open()
        await _seed(store, "run_old_open", finished=False, age_days=90)
        kept = await store.prune(older_than_days=30)
        swept = await store.prune(older_than_days=30, keep_unfinished=False)
        await store.close()
        return kept, swept

    kept, swept = run(main())
    assert kept == 0, "unfinished runs are retained by default"
    assert swept == 1


def test_prune_removes_checkpoints_with_the_run(store_path) -> None:
    """A checkpoint without its log is a pointer into nothing."""
    from forge.core.contracts import Checkpoint

    async def main():
        store = SQLiteEventStore(store_path)
        await store.open()
        await _seed(store, "run_old_done", finished=True, age_days=90)
        await store.write_checkpoint(
            Checkpoint(run_id="run_old_done", step_index=1, last_seq=2, state={})
        )
        await store.prune(older_than_days=30)
        orphan = await store.latest_checkpoint("run_old_done")
        await store.close()
        return orphan

    assert run(main()) is None


def test_prune_is_a_no_op_when_nothing_is_old_enough(store_path) -> None:
    async def main():
        store = SQLiteEventStore(store_path)
        await store.open()
        await _seed(store, "run_recent", finished=True, age_days=0)
        removed = await store.prune(older_than_days=30)
        still_there = await store.read("run_recent")
        await store.close()
        return removed, still_there

    removed, still_there = run(main())
    assert removed == 0
    assert still_there


def test_a_recently_resumed_old_run_is_not_pruned(store_path) -> None:
    """Eligibility is by *newest* event, not oldest.

    A run created months ago but resumed yesterday is live history; pruning it
    on creation date would delete a log someone is still using.
    """

    async def main():
        store = SQLiteEventStore(store_path)
        await store.open()
        await _seed(store, "run_revived", finished=True, age_days=90)
        # A fresh event lands today, after the backdating.
        await store.append(
            NewEvent(type=EventType.RUN_RESUMED, run_id="run_revived", payload={})
        )
        removed = await store.prune(older_than_days=30)
        await store.close()
        return removed

    assert run(main()) == 0
