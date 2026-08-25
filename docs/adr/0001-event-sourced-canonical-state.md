# ADR-0001: Canonical state is the fold of an append-only event log

- **Status:** accepted
- **Date:** 2026-08-25

## Context

A long-horizon agent run must survive worker death, and afterwards it must be
possible to answer "what actually happened, and why?" without re-running
anything.

The obvious approach is a mutable `runs` table updated as the run progresses.
It is simpler to write and it fails in a specific, expensive way: the moment
the crash-resume path reads state differently from the live path, the two
drift, and the drift only shows up under exactly the conditions you cannot
reproduce.

## Decision

Canonical state is **defined** as `project(events)` - a pure fold over an
append-only log. There is no authoritative mutable row anywhere. Checkpoints
are a cache of that fold plus a sequence watermark, never a separate truth.

Both the live runtime and `resume()` call the same `project()`.

## Consequences

**Good.** Crash-resume is not a second code path, so it cannot drift from the
first. The audit trail required by spec §16 is a by-product rather than extra
instrumentation. Replay (§19) becomes almost free: the log already contains
every proposal.

**Good.** The fold is total - an unrecognised event type advances the
watermark and is otherwise ignored - so an older runtime can read a newer log.
Forward-compatibility is cheap here and expensive to retrofit.

**Costs.** Reading current state is O(events since checkpoint) rather than one
row read. Mitigated by checkpointing every step by default. The log grows
without bound; compaction is deferred, and is a real obligation before this
runs anywhere serious.

**Costs.** Every state change needs an event type. This is friction, and it is
the useful kind: it forces "is this worth recording?" to be answered
explicitly.

## Alternatives considered

- **Mutable state table** - rejected above.
- **Event log as an audit side-channel, mutable state as truth.** The worst of
  both: two sources of truth, and the audit trail is the one nobody notices
  has gone stale.
