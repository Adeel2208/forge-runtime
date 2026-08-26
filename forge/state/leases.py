"""Run ownership leases.

A durable runtime is only durable end-to-end if something notices when a
worker dies. Leases are how a run knows who owns it, and how a supervisor
learns that nobody does any more.

The design is deliberately dumb, because ownership bugs are subtle:

* A lease is a row keyed on `run_id`. Claiming is an atomic conditional
  write, so two workers racing to claim the same run cannot both win - the
  database decides, not the application.
* A live worker heartbeats. A dead worker cannot, so its lease simply
  expires. There is no "am I still alive?" check to get wrong, and no
  shutdown hook a `SIGKILL` can skip.
* Expiry is evaluated against the *store's* clock, not the worker's, so a
  worker with a skewed clock cannot extend its own lease indefinitely.

This is a separate protocol from `EventStore` on purpose: the event log is
the record of what happened, and ownership is not part of that record
(ADR-0004 keeps the event store narrow).
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = ["Lease", "LeaseStore", "SQLiteLeaseStore"]

DEFAULT_TTL_S = 60.0


@dataclass(frozen=True)
class Lease:
    run_id: str
    owner: str
    expires_at: datetime
    claimed_at: datetime

    def is_live(self, now: datetime | None = None) -> bool:
        return self.expires_at > (now or datetime.now(UTC))


@runtime_checkable
class LeaseStore(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def claim(self, run_id: str, owner: str, *, ttl_s: float = DEFAULT_TTL_S) -> bool:
        """Take ownership. False if someone else holds a live lease.

        Must be atomic: concurrent claimants cannot both succeed.
        """
        ...

    async def heartbeat(self, run_id: str, owner: str, *, ttl_s: float = DEFAULT_TTL_S) -> bool:
        """Extend our own lease. False if we no longer hold it - which means
        a supervisor reclaimed the run and this worker must stop."""
        ...

    async def release(self, run_id: str, owner: str) -> None:
        """Give up ownership on clean completion."""
        ...

    async def expired(self, *, limit: int = 100) -> list[Lease]: ...

    async def get(self, run_id: str) -> Lease | None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_leases (
    run_id     TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_leases_expiry ON run_leases(expires_at);
"""


class SQLiteLeaseStore:
    """Implements `LeaseStore` on the same database file as the event store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        if self._conn is not None:
            return
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._connect)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        return conn

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("lease store is not open")
        return self._conn

    # -- claiming ----------------------------------------------------------

    async def claim(self, run_id: str, owner: str, *, ttl_s: float = DEFAULT_TTL_S) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._claim_sync, run_id, owner, ttl_s)

    def _claim_sync(self, run_id: str, owner: str, ttl_s: float) -> bool:
        conn = self._require()
        now = datetime.now(UTC)
        expires = (now + timedelta(seconds=ttl_s)).isoformat()

        # One statement, so the race is resolved by the database. The WHERE
        # clause is the whole safety argument: we take the row only if it is
        # unowned, already ours, or the previous owner's lease has lapsed.
        cur = conn.execute(
            "INSERT INTO run_leases (run_id, owner, claimed_at, expires_at, generation)"
            " VALUES (?,?,?,?,1)"
            " ON CONFLICT(run_id) DO UPDATE SET"
            "   owner=excluded.owner,"
            "   claimed_at=excluded.claimed_at,"
            "   expires_at=excluded.expires_at,"
            "   generation=run_leases.generation + 1"
            " WHERE run_leases.owner=excluded.owner OR run_leases.expires_at <= ?",
            (run_id, owner, now.isoformat(), expires, now.isoformat()),
        )
        return cur.rowcount > 0

    async def heartbeat(
        self, run_id: str, owner: str, *, ttl_s: float = DEFAULT_TTL_S
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._heartbeat_sync, run_id, owner, ttl_s)

    def _heartbeat_sync(self, run_id: str, owner: str, ttl_s: float) -> bool:
        conn = self._require()
        now = datetime.now(UTC)
        expires = (now + timedelta(seconds=ttl_s)).isoformat()
        cur = conn.execute(
            "UPDATE run_leases SET expires_at=? WHERE run_id=? AND owner=?",
            (expires, run_id, owner),
        )
        return cur.rowcount > 0

    async def release(self, run_id: str, owner: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._release_sync, run_id, owner)

    def _release_sync(self, run_id: str, owner: str) -> None:
        conn = self._require()
        conn.execute("DELETE FROM run_leases WHERE run_id=? AND owner=?", (run_id, owner))

    # -- inspection --------------------------------------------------------

    async def expired(self, *, limit: int = 100) -> list[Lease]:
        async with self._lock:
            rows = await asyncio.to_thread(self._expired_sync, limit)
        return [self._to_lease(r) for r in rows]

    def _expired_sync(self, limit: int) -> list[sqlite3.Row]:
        conn = self._require()
        return list(
            conn.execute(
                "SELECT * FROM run_leases WHERE expires_at <= ? ORDER BY expires_at LIMIT ?",
                (datetime.now(UTC).isoformat(), limit),
            )
        )

    async def get(self, run_id: str) -> Lease | None:
        async with self._lock:
            row = await asyncio.to_thread(self._get_sync, run_id)
        return self._to_lease(row) if row else None

    def _get_sync(self, run_id: str) -> sqlite3.Row | None:
        conn = self._require()
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM run_leases WHERE run_id=?", (run_id,)
        ).fetchone()
        return row

    @staticmethod
    def _to_lease(row: sqlite3.Row) -> Lease:
        return Lease(
            run_id=str(row["run_id"]),
            owner=str(row["owner"]),
            claimed_at=datetime.fromisoformat(str(row["claimed_at"])),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
        )
