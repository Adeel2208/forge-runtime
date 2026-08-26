"""The run supervisor: nothing is abandoned.

A runtime that resumes correctly is only half a durable system. Something has
to *notice* that a run stopped, and in a service that something cannot be the
process that died. The supervisor is that something.

    startup sweep   every unfinished run whose lease has lapsed is reclaimed
                    and resumed - including runs abandoned by a previous
                    deployment that no longer exists
    periodic reap   the same sweep on an interval, so a worker that dies
                    mid-flight is recovered without an operator noticing
    heartbeat       a live worker extends its lease; a dead one cannot, so
                    expiry needs no cooperation from the process that failed

Two properties matter more than the mechanics:

* **Recovery is idempotent.** Resuming re-enters the runtime's normal resume
  path, which is idempotency-protected at the effect level. A run reclaimed
  twice does not perform an effect twice.
* **Claiming is exclusive.** Two supervisors racing to recover the same run
  cannot both win; the lease store resolves it in one atomic write. Running
  several replicas is safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from forge.core.enums import RunStatus
from forge.runtime.loop import RunResult
from forge.state.leases import DEFAULT_TTL_S, LeaseStore
from forge.state.store import EventStore
from forge.telemetry.logging import get_logger

__all__ = ["Supervisor", "SupervisorConfig", "worker_identity"]

log = get_logger("forge.supervisor")


def worker_identity() -> str:
    """A stable-per-process owner id.

    Host and PID, so an operator reading a lease table can tell which box and
    which process held a run - and so a restarted process never accidentally
    inherits its own previous lease.
    """
    return f"{socket.gethostname()}/{os.getpid()}"


@dataclass
class SupervisorConfig:
    lease_ttl_s: float = DEFAULT_TTL_S
    heartbeat_interval_s: float = 20.0
    """Must be comfortably below `lease_ttl_s`, or a slow tick loses the lease."""

    sweep_interval_s: float = 30.0
    max_recoveries_per_sweep: int = 20
    recover_on_startup: bool = True
    max_recovery_attempts: int = 3
    """A run that fails to recover repeatedly is left alone and reported,
    rather than retried forever in a loop that looks like progress."""

    def __post_init__(self) -> None:
        if self.heartbeat_interval_s >= self.lease_ttl_s:
            raise ValueError(
                f"heartbeat_interval_s ({self.heartbeat_interval_s}) must be below "
                f"lease_ttl_s ({self.lease_ttl_s}), or a live worker loses its own lease"
            )


@dataclass
class Supervisor:
    """Owns run leases and recovers abandoned work."""

    store: EventStore
    leases: LeaseStore
    resume: Callable[[str], Awaitable[RunResult]]
    """How to continue a run. Injected so the supervisor never builds a
    runtime itself - it orchestrates recovery, it does not execute agents."""

    config: SupervisorConfig = field(default_factory=SupervisorConfig)
    owner: str = field(default_factory=worker_identity)

    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _heartbeats: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False)
    _attempts: dict[str, int] = field(default_factory=dict, init=False)
    _stopping: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    recovered: int = field(default=0, init=False)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Sweep once, then keep sweeping in the background."""
        if self.config.recover_on_startup:
            count = await self.sweep()
            if count:
                log.info("startup recovery", recovered=count, owner=self.owner)
        self._task = asyncio.create_task(self._loop(), name="forge-supervisor")

    async def stop(self, *, drain_timeout_s: float = 10.0) -> None:
        """Stop sweeping and release our leases.

        Releasing on the way out is a courtesy, not the safety mechanism: if
        this process is killed instead of shut down, the leases simply expire
        and another supervisor picks the work up. Shutdown hooks must never be
        the thing correctness depends on.
        """
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=drain_timeout_s)
            self._task = None

        for run_id, task in list(self._heartbeats.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            with contextlib.suppress(Exception):
                await self.leases.release(run_id, self.owner)
        self._heartbeats.clear()

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(self.config.sweep_interval_s)
                if self._stopping.is_set():
                    return
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("sweep failed", error=f"{type(exc).__name__}: {exc}")

    # -- ownership ---------------------------------------------------------

    async def claim(self, run_id: str) -> bool:
        """Take a run and start heartbeating for it."""
        if not await self.leases.claim(run_id, self.owner, ttl_s=self.config.lease_ttl_s):
            return False
        self._start_heartbeat(run_id)
        return True

    async def release(self, run_id: str) -> None:
        task = self._heartbeats.pop(run_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await self.leases.release(run_id, self.owner)

    def _start_heartbeat(self, run_id: str) -> None:
        async def beat() -> None:
            while True:
                await asyncio.sleep(self.config.heartbeat_interval_s)
                held = await self.leases.heartbeat(
                    run_id, self.owner, ttl_s=self.config.lease_ttl_s
                )
                if not held:
                    # Someone reclaimed this run - we were probably paused long
                    # enough for the lease to lapse. Stop pretending to own it.
                    log.warning("lost lease", run_id=run_id, owner=self.owner)
                    return

        self._heartbeats[run_id] = asyncio.create_task(beat(), name=f"hb-{run_id}")

    @contextlib.asynccontextmanager
    async def owning(self, run_id: str) -> Any:
        """Hold a lease for the duration of a block.

        Used by the API so an in-flight run is visibly owned; if the process
        dies inside the block, the lease lapses and a supervisor recovers it.
        """
        claimed = await self.claim(run_id)
        try:
            yield claimed
        finally:
            await self.release(run_id)

    # -- recovery ----------------------------------------------------------

    async def sweep(self) -> int:
        """Find abandoned runs and resume them. Returns how many were recovered."""
        candidates = await self.store.unfinished_runs(
            limit=self.config.max_recoveries_per_sweep * 4
        )
        recovered = 0

        for run_id in candidates:
            if self._stopping.is_set() or recovered >= self.config.max_recoveries_per_sweep:
                break
            if run_id in self._heartbeats:
                continue  # ours, and running
            if self._attempts.get(run_id, 0) >= self.config.max_recovery_attempts:
                continue  # giving up loudly is better than looping quietly

            lease = await self.leases.get(run_id)
            if lease is not None and lease.is_live():
                continue  # someone else is on it

            if await self.recover(run_id):
                recovered += 1

        return recovered

    async def recover(self, run_id: str) -> bool:
        """Reclaim and resume one run. False if another worker got there first."""
        if not await self.claim(run_id):
            return False

        self._attempts[run_id] = self._attempts.get(run_id, 0) + 1
        started = time.monotonic()
        try:
            result = await self.resume(run_id)
        except Exception as exc:
            log.error(
                "recovery failed",
                run_id=run_id,
                attempt=self._attempts[run_id],
                error=f"{type(exc).__name__}: {exc}",
            )
            return False
        finally:
            await self.release(run_id)

        self.recovered += 1
        self._attempts.pop(run_id, None)
        log.info(
            "recovered run",
            run_id=run_id,
            status=result.status.value,
            duplicate_effects=result.duplicate_effects,
            took_ms=int((time.monotonic() - started) * 1000),
        )
        if result.status is not RunStatus.COMPLETED:
            log.warning(
                "recovered run did not complete", run_id=run_id, status=result.status.value
            )
        return True

    # -- introspection -----------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "owned_runs": sorted(self._heartbeats),
            "recovered_total": self.recovered,
            "abandoned": sorted(
                k for k, v in self._attempts.items()
                if v >= self.config.max_recovery_attempts
            ),
        }
