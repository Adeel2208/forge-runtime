# The knowledge layer

Shared memory between runs, where nothing becomes true by being asserted.

A note is only promotable when a verifiable outcome in the event log supports
it, and only when the runs supplying that support did not get it from the note
itself. Agreement between correlated runs is not evidence; it is an echo.

The reasoning is in
[ADR-0007](adr/0007-knowledge-status-is-a-projection.md). This page is the
reference.

---

## Data model

```python
Anchor(path, region, content_hash)   # region: (start, end) lines, or a symbol name
Note(id, kind, scope, body, anchors, author_run_id, derived_from)
Attestation(note_id, run_id, event_seq, verdict, outcome, outcome_event, reader_lineage)
```

| Field | Why it exists |
|---|---|
| `Anchor.content_hash` | The region's hash **when the note was written**. Staleness is an exact comparison, not a guess about age. |
| `Note.body` | At most 500 characters, enforced at write. Longer insights are several notes, each corroborated on its own. |
| `Note.derived_from` | Contaminates in both directions — see the closure rule below. |
| `Attestation.event_seq` | Must resolve to a **terminal event of the attesting run**. Not an opinion: a pointer at something anyone can re-check. |
| `Attestation.reader_lineage` | What the run had in context. The whole independence rule keys off this. |

`NoteKind` is `LANDMARK | RECIPE | HAZARD | CONSTRAINT | MODEL_FACT`.

**There is no `status` field.** Status is computed by
`forge.knowledge.projection.project()`. A cached projection table is fine; a
stored status is the wrong turn, and `tests/knowledge/test_architecture.py`
fails if one appears.

---

## Events

All appended to the ordinary event log, under the ordinary
`(run_id, idempotency_key)` unique index.

```
NOTE_PROPOSED                 (note_id, kind, scope, body, anchors, author_run_id, derived_from, body_hash)
ATTESTATION_RECORDED          (note_id, run_id, event_seq, verdict, outcome, reader_lineage)
ATTESTATION_RETRACTED         (note_id, run_id, event_seq, verdict, actor, reason)
NOTE_QUARANTINED              (note_id, actor, reason)
NOTE_RELEASED                 (note_id, actor, reason)
NOTES_MERGED                  (surviving_id, absorbed_id, actor)
NOTE_REANCHORED               (note_id, anchors, observed_at, actor)
ADVERSARIAL_RETEST_RECORDED   (note_id, passed, actor, detail)
```

Idempotency keys:

```
note write   content_hash(author_run_id, canonical(body), anchors)
attestation  content_hash(note_id, run_id, event_seq, verdict)
```

Recording is the claim: one durable append against a unique index, never
read-then-write. A retried step recomputes the same key, loses the append, and
the count does not move.

> **The index is per-run, and that matters.** It is
> `UNIQUE(run_id, idempotency_key)`, so it cannot collapse *different* runs
> writing byte-identical bodies — four runs produce four keys in four scopes
> and four rows land. That collapse happens in the projection, keyed on
> `body_hash`. This is better than a store-level dedupe: it is reconstructible,
> and it survives a policy change without a migration.

---

## The promotion predicate

```
lineage_closure(L) = least fixed point of:
    L ⊆ C
    n ∈ C ⟹ derived_from(n) ⊆ C          # uphill: what n was built from
    n ∈ C ⟹ derived_by(n)   ⊆ C          # downhill: what was built from n
    n ∈ C ⟹ {survivor(n)} ∪ absorbed(n) ⊆ C

independent_support(X) = |{ a.run_id : a ∈ attestations(X)
                          , a.verdict = SUPPORT
                          , resolve(a.event_seq) = PASSING
                          , X ∉ lineage_closure(a.reader_lineage)
                          , a.run_id ∉ authors(X) }|
```

Evaluated in order — the first match wins:

| | Status | Condition |
|---|---|---|
| 1 | `QUARANTINED` | quarantined and not released |
| 2 | `STALE` | any anchor's hash ≠ the region's current hash |
| 3 | `REFUTED` | `independent_refute > independent_support` |
| 4 | `CANONICAL` | `support ≥ 4` and `refute = 0` and adversarial retest passed |
| 5 | `CORROBORATED` | `support ≥ 2` |
| 6 | `CLAIM` | otherwise |

Every threshold lives in `PromotionPolicy`; the numbers above are its defaults.

**Three things worth internalising.**

*One vote per run.* Five attestations from one run are one run's worth of
evidence. Everything after the first is discounted `DUPLICATE_RUN`.

*Authors never corroborate their own work.* This is **not** implied by lineage —
an authoring run never *read* its note, so `reader_lineage` is empty. It takes
an explicit clause, and after collapse `authors` is every run that wrote that
content.

*Downhill contamination.* If X derives from Y, a reader of Y cannot corroborate
X. Omit this and a note launders itself into independence by being restated.

Discounted evidence is kept, with its reason:

```python
state.notes[note_id].discounted_for("SELF_AUTHORED")
state.notes[note_id].discounted_for("LINEAGE_CONTAMINATED")
```

---

## A worked example

One note, one log. `CLAIM → CORROBORATED → CANONICAL → STALE`.

**A run discovers something.**

```python
await knowledge_write(
    kind="HAZARD",
    body="retry() swallows OSError only; a ValueError from fn propagates uncaught",
    path="forge/retry.py",
    region="retry",
)
```

`NOTE_PROPOSED` lands, anchored to the hash of the `retry` function as it
stands. Status: **`CLAIM`** — one run said so, nothing has tested it.

**Two unrelated runs hit the same thing and finish.**

```python
await knowledge_attest(note_id, event_seq=my_terminal_seq, verdict="SUPPORT")
```

Neither had the note in context, neither authored it, both cite a
`RUN_COMPLETED`. `independent_support = 2` → **`CORROBORATED`**.

A third run *did* read the note first, succeeded, and attests. It is discounted
`LINEAGE_CONTAMINATED`, and the count stays at 2. Its success says the note was
not fatal, which is not the same as saying it is true.

**Two more independent runs attest.** `independent_support = 4`. Still
**`CORROBORATED`**: four runs agreeing is not the same as anyone having checked.

**A Librarian re-tests it adversarially.**

```python
await knowledge_promote(note_id, passed=True, detail="fed ValueError; propagated uncaught")
```

Now `support ≥ 4`, `refute = 0`, retest passed → **`CANONICAL`**. Had the
note's own author recorded that retest, it would be ignored and the status
would stay `CORROBORATED`.

**Someone wraps the call in `except Exception`.** The anchored region's hash
changes. Status: **`STALE`** — immediately, on the next projection, with no job
to run. Three fresh supporting attestations do *not* revive it; agreement about
code that no longer exists in that form is agreement about nothing. Only a
`NOTE_REANCHORED` event brings it back, and then it returns to `CANONICAL`
because the evidence was never deleted.

Every transition above is the same function over the same log. Nothing was
written to a status field, because there is no status field.

---

## Using it

```python
from forge.knowledge import PromotionPolicy, SQLiteKnowledgeLog, project

log = SQLiteKnowledgeLog(store, path="forge.db")
events = await log.read_knowledge()          # across every run
state = project(events, tree_snapshot, PromotionPolicy())

state.status(note_id)                        # "CANONICAL"
state.notes[note_id].reason                  # "4 independent supporting runs, ..."
state.notes[note_id].discounted              # what did not count, and why
```

Raising the bar re-reads history; it does not migrate it:

```python
strict = project(events, tree_snapshot, PromotionPolicy(canonical_support=8))
```

## Capabilities

| Tool | Capability | Available to |
|---|---|---|
| `knowledge.write` | `KNOWLEDGE_WRITE` | any agent |
| `knowledge.attest` | `KNOWLEDGE_ATTEST` | any agent |
| `knowledge.read` | `KNOWLEDGE_READ` | any agent |
| `knowledge.promote` | `KNOWLEDGE_PROMOTE` | Librarian only |
| `knowledge.reanchor` | `KNOWLEDGE_PROMOTE` | Librarian only |
| `knowledge.quarantine` | `KNOWLEDGE_PROMOTE` | Librarian only |

`AGENT_TOOLS` and `LIBRARIAN_TOOLS` are separate registries with disjoint names
and disjoint capabilities, so a worker's tool surface does not contain the
promote symbol at all. A denied attempt is recorded as evidence and the run
continues, consistent with the runtime's existing policy-denial edge.

Run identity is bound by the runtime through `bind_session`, never taken from a
tool argument — a model that could name its own `run_id` could attest as any
run it liked, and disjointness would mean nothing.
