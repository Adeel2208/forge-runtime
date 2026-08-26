"""Supervisor and lease correctness - the service-boundary durability gate.

The runtime already resumes correctly. These tests defend the other half:
that something *notices* an abandoned run, that only one worker recovers it,
and that recovering it twice still performs each effect once.
"""

from __future__ import annotations

import asyncio

import pytest

from forge.core.contracts import TaskSpec
from forge.core.enums import EventType
from forge.evaluation.faults import FaultClass, FaultInjector
from forge.runtime.loop import SimulatedCrash
from forge.runtime.supervisor import Supervisor, SupervisorConfig
from forge.state.leases import SQLiteLeaseStore
from forge.state.sqlite_store import SQLiteEventStore
from tests.conftest import run

pytestmark = pytest.mark.recovery

SCRIPT = [
    {"proposal": {"kind": "TOOL_CALL", "tool": "calculate",
                  "arguments": {"expression": "10 + 5"}}},
    {"proposal": {"kind": "TOOL_CALL", "tool": "save_note",
                  "arguments": {"name": "a", "content": "fifteen"}}},
    {"proposal": {"kind": "TOOL_CALL", "tool": "save_note",
                  "arguments": {"name": "b", "content": "final"}}},
    {"proposal": {"kind": "ANSWER", "answer": "15, saved."}},
]
TOOLS = ["calculate", "save_note"]


# ── leases ──────────────────────────────────────────────────────────────


def test_only_one_worker_can_hold_a_lease(store_path) -> None:
    async def main():
        leases = SQLiteLeaseStore(store_path)
        await leases.open()
        first = await leases.claim("run_1", "worker-a", ttl_s=60)
        second = await leases.claim("run_1", "worker-b", ttl_s=60)
        await leases.close()
        return first, second

    first, second = run(main())
    assert first is True
    assert second is False, "a live lease must not be stealable"


def test_concurrent_claims_produce_exactly_one_winner(store_path) -> None:
    """The database resolves the race, not the application."""

    async def main():
        leases = SQLiteLeaseStore(store_path)
        await leases.open()
        results = await asyncio.gather(
            *(leases.claim("run_1", f"worker-{i}", ttl_s=60) for i in range(12))
        )
        await leases.close()
        return results

    assert sum(run(main())) == 1


def test_an_expired_lease_is_reclaimable(store_path) -> None:
    """A dead worker cannot release its lease, so expiry must suffice."""

    async def main():
        leases = SQLiteLeaseStore(store_path)
        await leases.open()
        await leases.claim("run_1", "dead-worker", ttl_s=0.05)
        await asyncio.sleep(0.12)
        stolen = await leases.claim("run_1", "live-worker", ttl_s=60)
        lease = await leases.get("run_1")
        await leases.close()
        return stolen, lease

    stolen, lease = run(main())
    assert stolen is True
    assert lease is not None and lease.owner == "live-worker"


def test_heartbeat_extends_only_your_own_lease(store_path) -> None:
    async def main():
        leases = SQLiteLeaseStore(store_path)
        await leases.open()
        await leases.claim("run_1", "worker-a", ttl_s=60)
        mine = await leases.heartbeat("run_1", "worker-a", ttl_s=60)
        theirs = await leases.heartbeat("run_1", "worker-b", ttl_s=60)
        await leases.close()
        return mine, theirs

    mine, theirs = run(main())
    assert mine is True
    assert theirs is False, "a worker must not extend a lease it does not hold"


def test_release_frees_the_run(store_path) -> None:
    async def main():
        leases = SQLiteLeaseStore(store_path)
        await leases.open()
        await leases.claim("run_1", "worker-a", ttl_s=60)
        await leases.release("run_1", "worker-a")
        reclaimed = await leases.claim("run_1", "worker-b", ttl_s=60)
        await leases.close()
        return reclaimed

    assert run(main()) is True


# ── finding abandoned work ──────────────────────────────────────────────


def test_unfinished_runs_excludes_completed_ones(make_runtime, store_path) -> None:
    async def main():
        store = SQLiteEventStore(store_path)
        await store.open()

        finished = make_runtime(SCRIPT, store=store)
        done = await finished.start(TaskSpec(goal="finish me", tools=TOOLS, max_steps=8))

        crashed = make_runtime(
            SCRIPT, store=store,
            faults=FaultInjector.single(FaultClass.WORKER_CRASH, at_step=2),
        )
        with pytest.raises(SimulatedCrash):
            await crashed.start(TaskSpec(goal="abandon me", tools=TOOLS, max_steps=8))

        unfinished = await store.unfinished_runs()
        await store.close()
        return done.run_id, unfinished

    finished_id, unfinished = run(main())
    assert finished_id not in unfinished, "a completed run is not abandoned work"
    assert len(unfinished) == 1


# ── recovery ────────────────────────────────────────────────────────────


def test_supervisor_recovers_an_abandoned_run(make_runtime, store_path) -> None:
    """The headline: a worker dies, and nobody has to notice by hand."""

    async def main():
        store = SQLiteEventStore(store_path)
        leases = SQLiteLeaseStore(store_path)
        await store.open()
        await leases.open()

        crashed = make_runtime(
            SCRIPT, store=store,
            faults=FaultInjector.single(FaultClass.WORKER_CRASH, at_step=3),
        )
        with pytest.raises(SimulatedCrash):
            await crashed.start(TaskSpec(goal="crash", tools=TOOLS, max_steps=8))
        run_id = str((await store.list_runs(limit=1))[0]["run_id"])

        supervisor = Supervisor(
            store=store,
            leases=leases,
            resume=lambda rid: make_runtime(SCRIPT, store=store).resume(rid),
            config=SupervisorConfig(recover_on_startup=True, sweep_interval_s=3600),
            owner="supervisor-1",
        )
        recovered = await supervisor.sweep()
        events = await store.read(run_id)

        await supervisor.stop()
        await leases.close()
        await store.close()
        return recovered, events

    recovered, events = run(main())
    performed = [e for e in events if e.type is EventType.EFFECT_OBSERVED]
    keys = [e.idempotency_key for e in performed]

    assert recovered == 1
    assert any(e.type is EventType.RUN_COMPLETED for e in events)
    assert len(keys) == len(set(keys)), f"recovery duplicated an effect: {keys}"


def test_supervisor_will_not_touch_a_live_lease(make_runtime, store_path) -> None:
    """Another replica is already on it. Two recoveries would race."""

    async def main():
        store = SQLiteEventStore(store_path)
        leases = SQLiteLeaseStore(store_path)
        await store.open()
        await leases.open()

        crashed = make_runtime(
            SCRIPT, store=store,
            faults=FaultInjector.single(FaultClass.WORKER_CRASH, at_step=2),
        )
        with pytest.raises(SimulatedCrash):
            await crashed.start(TaskSpec(goal="crash", tools=TOOLS, max_steps=8))
        run_id = str((await store.list_runs(limit=1))[0]["run_id"])

        await leases.claim(run_id, "another-replica", ttl_s=60)

        resumed = []

        async def should_not_run(rid: str):
            resumed.append(rid)
            raise AssertionError("must not recover a run someone else owns")

        supervisor = Supervisor(
            store=store, leases=leases, resume=should_not_run,
            config=SupervisorConfig(recover_on_startup=False), owner="supervisor-1",
        )
        recovered = await supervisor.sweep()
        await supervisor.stop()
        await leases.close()
        await store.close()
        return recovered, resumed

    recovered, resumed = run(main())
    assert recovered == 0
    assert resumed == []


def test_two_supervisors_recover_a_run_exactly_once(make_runtime, store_path) -> None:
    """Running several replicas must be safe."""

    async def main():
        store = SQLiteEventStore(store_path)
        leases = SQLiteLeaseStore(store_path)
        await store.open()
        await leases.open()

        crashed = make_runtime(
            SCRIPT, store=store,
            faults=FaultInjector.single(FaultClass.WORKER_CRASH, at_step=3),
        )
        with pytest.raises(SimulatedCrash):
            await crashed.start(TaskSpec(goal="crash", tools=TOOLS, max_steps=8))

        attempts: list[str] = []

        def make_supervisor(name: str) -> Supervisor:
            async def resume(rid: str):
                attempts.append(name)
                return await make_runtime(SCRIPT, store=store).resume(rid)

            return Supervisor(
                store=store, leases=leases, resume=resume,
                config=SupervisorConfig(recover_on_startup=False), owner=name,
            )

        a, b = make_supervisor("sup-a"), make_supervisor("sup-b")
        counts = await asyncio.gather(a.sweep(), b.sweep())
        await a.stop()
        await b.stop()

        run_id = str((await store.list_runs(limit=1))[0]["run_id"])
        events = await store.read(run_id)
        await leases.close()
        await store.close()
        return counts, attempts, events

    counts, attempts, events = run(main())
    performed = [e for e in events if e.type is EventType.EFFECT_OBSERVED]
    keys = [e.idempotency_key for e in performed]

    assert sum(counts) == 1, f"exactly one supervisor must recover the run, got {counts}"
    assert len(attempts) == 1
    assert len(keys) == len(set(keys)), "concurrent supervisors duplicated an effect"


def test_recovery_gives_up_after_repeated_failures(make_runtime, store_path) -> None:
    """Retrying forever looks like progress and isn't."""

    async def main():
        store = SQLiteEventStore(store_path)
        leases = SQLiteLeaseStore(store_path)
        await store.open()
        await leases.open()

        crashed = make_runtime(
            SCRIPT, store=store,
            faults=FaultInjector.single(FaultClass.WORKER_CRASH, at_step=2),
        )
        with pytest.raises(SimulatedCrash):
            await crashed.start(TaskSpec(goal="crash", tools=TOOLS, max_steps=8))

        calls = {"n": 0}

        async def always_fails(rid: str):
            calls["n"] += 1
            raise RuntimeError("resume is broken")

        supervisor = Supervisor(
            store=store, leases=leases, resume=always_fails,
            config=SupervisorConfig(recover_on_startup=False, max_recovery_attempts=2),
            owner="sup",
        )
        for _ in range(5):
            await supervisor.sweep()
        stats = supervisor.stats()
        await supervisor.stop()
        await leases.close()
        await store.close()
        return calls["n"], stats

    attempts, stats = run(main())
    assert attempts == 2, "must stop retrying after the configured ceiling"
    assert stats["abandoned"], "a run it gave up on must be visible, not silent"


def test_heartbeat_interval_must_be_below_the_ttl() -> None:
    """A misconfiguration that would make a live worker lose its own lease."""
    with pytest.raises(ValueError, match="must be below"):
        SupervisorConfig(lease_ttl_s=10, heartbeat_interval_s=30)
