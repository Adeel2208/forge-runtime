"""SQLite-backed event store: the default, zero-infrastructure backend.

Durability notes, since crash-resume correctness depends on them:

* WAL journalling with ``synchronous=FULL`` - an appended event has reached
  disk before ``append()`` returns. A ``kill -9`` immediately afterwards
  cannot lose it, which is precisely what the recovery tests exercise.
* The idempotency index is a UNIQUE constraint, not an application check.
  Two concurrent workers racing to record the same effect cannot both win;
  the loser gets an `IntegrityError` and reads back the winner's row.

All blocking calls run on a worker thread so the event loop is never stalled.
A single connection behind an `asyncio.Lock` gives serialised writes, which is
correct and is not the bottleneck at this scale.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forge.core.contracts import Checkpoint
from forge.core.enums import KNOWLEDGE_EVENT_TYPES, EventType
from forge.core.events import Event, NewEvent
from forge.state.store import AppendResult

__all__ = ["SQLiteEventStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL,
    step_id         TEXT,
    step_index      INTEGER,
    type            TEXT    NOT NULL,
    payload         TEXT    NOT NULL,
    idempotency_key TEXT,
    ts              TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id, seq);

-- The heart of exactly-once effect intent: one effect per key per run.
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_idem
    ON events(run_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS checkpoints (
    id          TEXT PRIMARY KEY,
    run_id      TEXT    NOT NULL,
    step_index  INTEGER NOT NULL,
    last_seq    INTEGER NOT NULL,
    state       TEXT    NOT NULL,
    context_digest TEXT NOT NULL DEFAULT '',
    kind        TEXT    NOT NULL DEFAULT 'semantic',
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ckpt_run ON checkpoints(run_id, last_seq DESC);
"""


class SQLiteEventStore:
    """Implements `forge.state.store.EventStore`."""

    def __init__(self, path: str | Path = "forge.db") -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

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
        conn.execute("PRAGMA synchronous=FULL")   # durability over throughput
        conn.execute("PRAGMA foreign_keys=ON")
        # Multiple processes may share this file (an API replica and a
        # supervisor, say). Wait for the writer rather than failing fast.
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        return conn

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("event store is not open; call await store.open()")
        return self._conn

    # -- append ------------------------------------------------------------

    async def append(self, event: NewEvent) -> AppendResult:
        async with self._lock:
            return await asyncio.to_thread(self._append_sync, event)

    def _append_sync(self, event: NewEvent) -> AppendResult:
        conn = self._require()
        ts = datetime.now(UTC)
        row = (
            event.run_id,
            event.step_id,
            event.step_index,
            event.type.value,
            json.dumps(event.payload, default=str),
            event.idempotency_key,
            ts.isoformat(),
        )
        try:
            cur = conn.execute(
                "INSERT INTO events (run_id, step_id, step_index, type, payload,"
                " idempotency_key, ts) VALUES (?,?,?,?,?,?,?)",
                row,
            )
        except sqlite3.IntegrityError:
            # Lost the race, or this is a resumed run replaying a known effect.
            # Either way the authoritative record already exists: read it back.
            existing = self._find_effect_sync(event.run_id, event.idempotency_key or "")
            if existing is None:  # pragma: no cover - constraint we do not define
                raise
            return AppendResult(existing, deduplicated=True)

        return AppendResult(
            Event(seq=int(cur.lastrowid or 0), ts=ts, **event.model_dump()),
            deduplicated=False,
        )

    # -- read --------------------------------------------------------------

    async def read(self, run_id: str, *, after_seq: int = 0) -> list[Event]:
        async with self._lock:
            rows = await asyncio.to_thread(self._read_sync, run_id, after_seq)
        return [self._to_event(r) for r in rows]

    def _read_sync(self, run_id: str, after_seq: int) -> list[sqlite3.Row]:
        conn = self._require()
        return list(
            conn.execute(
                "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq",
                (run_id, after_seq),
            )
        )

    async def find_effect(self, run_id: str, idempotency_key: str) -> Event | None:
        async with self._lock:
            return await asyncio.to_thread(self._find_effect_sync, run_id, idempotency_key)

    def _find_effect_sync(self, run_id: str, idempotency_key: str) -> Event | None:
        if not idempotency_key:
            return None
        conn = self._require()
        row = conn.execute(
            "SELECT * FROM events WHERE run_id=? AND idempotency_key=? LIMIT 1",
            (run_id, idempotency_key),
        ).fetchone()
        return self._to_event(row) if row else None

    @staticmethod
    def _to_event(row: sqlite3.Row) -> Event:
        return Event(
            seq=int(row["seq"]),
            run_id=str(row["run_id"]),
            step_id=row["step_id"],
            step_index=row["step_index"],
            type=EventType(row["type"]),
            payload=json.loads(row["payload"]),
            idempotency_key=row["idempotency_key"],
            ts=datetime.fromisoformat(str(row["ts"])),
        )

    # -- checkpoints -------------------------------------------------------

    async def write_checkpoint(self, checkpoint: Checkpoint) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write_ckpt_sync, checkpoint)

    def _write_ckpt_sync(self, ckpt: Checkpoint) -> None:
        conn = self._require()
        conn.execute(
            "INSERT OR REPLACE INTO checkpoints"
            " (id, run_id, step_index, last_seq, state, context_digest, kind, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                ckpt.id,
                ckpt.run_id,
                ckpt.step_index,
                ckpt.last_seq,
                json.dumps(ckpt.state, default=str),
                ckpt.context_digest,
                ckpt.kind,
                (ckpt.created_at or datetime.now(UTC)).isoformat(),
            ),
        )

    async def latest_checkpoint(self, run_id: str) -> Checkpoint | None:
        async with self._lock:
            row = await asyncio.to_thread(self._latest_ckpt_sync, run_id)
        if row is None:
            return None
        return Checkpoint(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            step_index=int(row["step_index"]),
            last_seq=int(row["last_seq"]),
            state=json.loads(row["state"]),
            context_digest=str(row["context_digest"]),
            kind=str(row["kind"]),  # type: ignore[arg-type]
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def _latest_ckpt_sync(self, run_id: str) -> sqlite3.Row | None:
        conn = self._require()
        # step_index breaks ties: two checkpoints can share a watermark if a
        # step wrote no events between them, and picking the older one would
        # silently rewind the run.
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM checkpoints WHERE run_id=?"
            " ORDER BY last_seq DESC, step_index DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return row

    # -- introspection -----------------------------------------------------

    async def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self._lock:
            rows = await asyncio.to_thread(self._list_runs_sync, limit)
        return [dict(r) for r in rows]

    async def prune(self, *, older_than_days: float, keep_unfinished: bool = True) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._prune_sync, older_than_days, keep_unfinished)

    def _prune_sync(self, older_than_days: float, keep_unfinished: bool) -> int:
        from datetime import timedelta

        conn = self._require()
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()

        terminal = "('RUN_COMPLETED','RUN_FAILED','RUN_ABORTED')"
        # Eligible = every event for the run predates the cutoff, so an old run
        # that was resumed recently is not silently truncated mid-history.
        condition = f"SELECT run_id FROM events GROUP BY run_id HAVING MAX(ts) < '{cutoff}'"
        if keep_unfinished:
            condition += (
                f" AND SUM(CASE WHEN type IN {terminal} THEN 1 ELSE 0 END) > 0"
            )

        victims = [str(r["run_id"]) for r in conn.execute(condition)]
        if not victims:
            return 0

        placeholders = ",".join("?" * len(victims))
        # Knowledge events survive the run that wrote them. They are facts the
        # run established, not state belonging to it - deleting them would let
        # routine retention silently destroy the shared knowledge base, taking
        # corroborated notes and the attestations pointing at them.
        #
        # This does not violate the whole-runs rule above. `project()` ignores
        # knowledge events entirely, so a run whose runtime events are gone
        # projects to nothing either way, and `unfinished_runs` keys on
        # RUN_CREATED, so what remains cannot masquerade as resumable work.
        keep = sorted(t.value for t in KNOWLEDGE_EVENT_TYPES)
        keep_slots = ",".join("?" * len(keep))
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                f"DELETE FROM events WHERE run_id IN ({placeholders})"
                f" AND type NOT IN ({keep_slots})",
                (*victims, *keep),
            )
            conn.execute(f"DELETE FROM checkpoints WHERE run_id IN ({placeholders})", victims)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return len(victims)

    async def unfinished_runs(self, *, limit: int = 100) -> list[str]:
        async with self._lock:
            rows = await asyncio.to_thread(self._unfinished_sync, limit)
        return [str(r["run_id"]) for r in rows]

    def _unfinished_sync(self, limit: int) -> list[sqlite3.Row]:
        conn = self._require()
        # A run is unfinished if it was created and has no terminal event.
        # Ordered oldest-first so the longest-abandoned work is recovered
        # before work that only just stopped.
        return list(
            conn.execute(
                "SELECT run_id, MIN(seq) AS started FROM events"
                " WHERE run_id IN (SELECT run_id FROM events WHERE type='RUN_CREATED')"
                "   AND run_id NOT IN ("
                "       SELECT run_id FROM events"
                "       WHERE type IN ('RUN_COMPLETED','RUN_FAILED','RUN_ABORTED')"
                "   )"
                " GROUP BY run_id ORDER BY started LIMIT ?",
                (limit,),
            )
        )

    def _list_runs_sync(self, limit: int) -> list[sqlite3.Row]:
        conn = self._require()
        # Status is derived in SQL from the presence of a terminal event rather
        # than by projecting each run. A listing of fifty runs would otherwise
        # be fifty folds, and the answer to "did it finish" does not need one:
        # the terminal event either exists or it does not. Anything without one
        # is still in flight or was interrupted, and the run view - which does
        # project - is what distinguishes those.
        return list(
            conn.execute(
                "SELECT run_id,"
                "       MIN(ts)   AS started,"
                "       MAX(ts)   AS updated,"
                "       COUNT(*)  AS events,"
                "       MAX(seq)  AS last_seq,"
                "       COALESCE(MAX(CASE type"
                "           WHEN 'RUN_COMPLETED' THEN 'COMPLETED'"
                "           WHEN 'RUN_FAILED'    THEN 'FAILED'"
                "           WHEN 'RUN_ABORTED'   THEN 'ABORTED'"
                "       END), 'RUNNING') AS status"
                " FROM events GROUP BY run_id ORDER BY last_seq DESC LIMIT ?",
                (limit,),
            )
        )
