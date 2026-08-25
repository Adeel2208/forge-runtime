# ADR-0004: SQLite is the default event store; Postgres is a backend

- **Status:** accepted
- **Date:** 2026-08-25

## Context

The specification names PostgreSQL. Postgres is the right answer for
horizontal workers and concurrent runs, and this project will want it.

But the reference stack has a cost that is easy to overlook: it makes
`docker compose up` a prerequisite for running a single test. The development
machine for v0.1 has 16 GB of RAM, no Docker installed, and needs to run a
local 8B model in VRAM at the same time. Requiring Postgres to execute the
test suite would have made the fast loop slow and the contribution barrier
high - for a v0.1 whose entire point is the *logic* above storage.

## Decision

Define a narrow `EventStore` protocol - five methods - and ship
`SQLiteEventStore` as the default. Postgres becomes an alternative
implementation of the same protocol rather than a prerequisite.

SQLite is configured for durability over throughput:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;   -- an appended event has reached disk on return
CREATE UNIQUE INDEX ux_events_idem ON events(run_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
```

`synchronous=FULL` is what makes the hard-kill recovery test meaningful: a
`os._exit()` immediately after `append()` returns cannot lose the event.

## Consequences

**Good.** `pytest` runs on a clean checkout with no services. The suite is
~15 seconds, which keeps the loop tight enough to actually use.

**Good.** The protocol boundary is now proven rather than assumed. Writing it
against SQLite first forced it to stay narrow - had we started with Postgres,
connection pools and transaction scopes would have leaked into the interface.

**Good.** The idempotency guarantee is a database `UNIQUE` constraint in both
backends, not an application-level check. Concurrency correctness does not
depend on which store is mounted.

**Costs.** SQLite serialises writes behind a single connection and an
`asyncio.Lock`. Correct, but it caps concurrent runs per process. This is the
reason Postgres exists on the roadmap, and the reason it is not urgent yet.

**Costs.** Two backends is two implementations to keep honest. Mitigated by
the protocol being five methods; the store tests are written against the
protocol so they will run unchanged against Postgres.

## Alternatives considered

- **Postgres only.** Rejected: makes the test loop depend on infrastructure
  the v0.1 logic does not need.
- **SQLite only.** Rejected: gives up the horizontal-worker story the spec
  explicitly wants, and would let pool-shaped assumptions calcify.
- **An ORM over both.** Rejected: an ORM would obscure the exact atomicity and
  constraint semantics that the whole crash-safety argument rests on. This is
  the one place where writing the SQL by hand is the safer choice.
