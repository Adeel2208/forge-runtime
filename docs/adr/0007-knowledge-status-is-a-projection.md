# ADR-0007: Knowledge status is a projection, not stored state

- **Status:** accepted
- **Date:** 2026-08-27

## Context

Multiple runs working on one repository learn things. A run discovers that a
retry path reuses its idempotency key, that a module is not thread-safe, that
a particular model reliably mangles multi-line edits. Today that dies with the
run. The obvious fix is a shared notes table any agent can write to.

The obvious fix does not work, and it fails in a way that looks like success.
Once agents can write to a shared store and read from it, three things happen.
A note gets written; other runs read it; those runs succeed; their success is
counted as evidence for the note. The store converges on whatever was written
first, and it converges *confidently*, because by then the note has a dozen
corroborations. Nothing detects this. Every individual step is reasonable.

The failure is not dishonesty. It is correlation. A run that had X in context
and then succeeded is not evidence for X — it is evidence that X was not
fatal. Those are different claims, and only the second is licensed by the
observation. Any scheme that conflates them will manufacture consensus out of
exposure.

## Decision

**A note's status is never stored.** It is computed by folding the event log:
`project(events, tree_snapshot, policy)`. There is no `status` column, no
`promote()` that assigns a value, and no writable field anywhere that a status
could be put into. Promotion is a conclusion the fold reaches.

Three rules make the conclusion mean something.

**1. Evidence must point at a verifiable outcome.** An attestation carries an
`event_seq` that must resolve to a terminal event of the attesting run. It is
not an opinion; it is a pointer at something that already happened and that
anyone reading the same log can re-check. A `SUPPORT` attestation citing a
failing outcome is refused at write time, not filtered at read time — evidence
the projection would have to ignore should never have been recorded.

**2. Corroboration requires provenance disjointness.** A run's attestation
counts only when the note is not in the closure of what that run read:

```
independent_support(X) = |{ a.run_id : a ∈ attestations(X)
                          , a.verdict = SUPPORT
                          , resolve(a.event_seq) = PASSING
                          , X ∉ lineage_closure(a.reader_lineage)
                          , a.run_id ∉ authors(X) }|
```

`lineage_closure` follows derivation and merge edges in **both** directions.
Uphill is obvious: reading X means reading what X was built from. Downhill is
the one that is easy to miss and expensive to omit — if X derives from Y, a
reader of Y is not independent evidence for X, because part of what X asserts
*is* Y. Without the downhill edge a note launders itself into independence by
being restated: write Y, derive X from it, and every reader of Y becomes a
fresh corroborator of X.

`authors(X)` is a set, not a single run. Byte-identical bodies from different
runs collapse into one note, and every run that wrote that content authored the
survivor. Excluding only the elected survivor's author would leave the
sockpuppet attack intact: write the same sentence from four runs, attest from
each, and three read as independent corroboration of the fourth.

**3. Anchoring is by content hash, not by age.** A note about code is true
about *a piece of code*, not about a point in time. "This function is not
thread-safe" does not become less true because it is six months old, and it
becomes false the instant someone adds a lock. Time-decay gets both cases
wrong. An anchor stores the hash of the region as it was; staleness is an exact
comparison. Staleness dominates corroboration, because agreement about code
that no longer exists in that form is agreement about nothing.

Precedence, evaluated in order: `QUARANTINED`, `STALE`, `REFUTED`,
`CANONICAL`, `CORROBORATED`, `CLAIM`. Quarantine outranks staleness because a
deliberate human act should outrank a mechanical observation.

## Consequences

**Good.** Policy is retroactive. `PromotionPolicy` is a frozen dataclass passed
into the fold, so raising the corroboration threshold re-evaluates all of
history with no migration and no rewrite — the same events, read differently.
There is no cached status to become inconsistent with the log, which is the
strongest available form of "checkpoints are disposable" (ADR-0001): there is
nothing to drop.

**Good.** Self-promotion is unrepresentable rather than forbidden. There is no
tool that sets `CANONICAL`, so "a run promoted its own note" is not a policy
violation to detect — it is a sentence that does not correspond to any
operation. `knowledge.promote` records an adversarial retest, and the
projection ignores a retest whose actor authored the note. The capability gate
stops the wrong role; this stops the right role acting on its own work.

**Good.** Discounted evidence is recorded with its reason rather than dropped.
A store that quietly ignores evidence is indistinguishable from one that is
broken, and `NoteState.discounted_for("SELF_AUTHORED")` is what lets the
adversarial fixtures assert *why* something failed to promote rather than
merely that it did.

**Cost.** Reading status is O(all knowledge events), not one row. Worse, unlike
`RunState` this **cannot be checkpointed**, and that is not an omission. A run's
state depends only on its own prefix, so a checkpoint plus a watermark is
sound. Knowledge status does not: a merge, a retraction or a late attestation
changes the count for events folded long before it, so no prefix has a
projection that stays valid. The mitigation is scope — knowledge event volume
is tiny next to step events — and compaction is a real obligation before this
runs at scale.

**Cost.** Corroboration is slow, deliberately. Four independent runs that never
read the note is a high bar, and broad derivation families corroborate more
slowly still, because siblings sharing a parent contaminate each other. A
knowledge layer that promoted quickly would be one that promoted wrongly.

**Cost.** Unanchored notes (`scope: repo`, `MODEL_FACT`) can never go stale,
because nothing in the tree can contradict them. They rest entirely on
corroboration and refutation, which is weaker than the anchored case.

## What would falsify this

Stated plainly, because a design that cannot be wrong is not a design.

**The causal-correlation gap is real and currently open.** Disjointness catches
*provenance* correlation — did this run read the note. It does not catch
*causal* correlation. Two runs forked from the same parent, given the same
goal, driven by the same model at the same commit, are not independent in any
meaningful sense, and this predicate counts them as two. Closing it needs run
genealogy (`parent_run_id`), which the runtime does not record. Until then the
independence claim is narrower than it sounds, and the honest reading of
`independent_support = 4` is "four runs that had not read this", not "four
independent confirmations".

**Symbol anchoring is a lexical heuristic, not a parser.** It matches
definition forms for C-family and Python sources. A language it does not know,
or a symbol defined unusually, resolves to nothing and the note reads as stale.
That is the safe direction, but a language whose definitions it cannot see
makes every note about that language permanently stale.

**Concretely, the design is wrong if** notes reach `CANONICAL` and are then
refuted at a rate materially above the base rate for `CORROBORATED` ones —
that would mean the adversarial retest is theatre. It is also wrong if, after
real use, the `CANONICAL` set is dominated by notes that no run ever acted on:
that would mean the layer is measuring agreement rather than usefulness, and
promotion should be gated on a note having *changed* a run's behaviour, not on
runs having succeeded near it.

## Alternatives considered

- **A shared notes table with a status column.** Rejected above: it converges
  on whatever was written first, confidently.

- **Voting without disjointness.** Cheap, and it makes exposure look like
  evidence. This is the specific failure the whole layer exists to avoid.

- **Time-decay instead of content anchoring.** Wrong in both directions: it
  expires facts that are still true and retains facts that were invalidated by
  the commit that landed a minute ago.

- **LLM-judged promotion.** Attractive, and it puts an unverifiable step at the
  centre of a mechanism whose entire purpose is verifiability. A grader may
  *abstain*; it may not manufacture the evidence it is grading.

- **Status stored, with a periodic reconciliation job.** The worst of both:
  two sources of truth, and the one nobody watches is the one that goes stale.
  This is the same trap ADR-0001 rejected for run state, and it is no better
  here.
