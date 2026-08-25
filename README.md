# FORGE

**A durable, policy-aware execution runtime for long-horizon AI agents.**

FORGE is not another agent framework. It is the layer *between* a model's
output and a real-world side effect: the part that has to be right when a
worker dies mid-write, when a model proposes something it was never authorized
to do, or when the same action arrives twice.

<p align="center">
  <img src="docs/assets/forge-demo.gif"
       alt="A FORGE run executing. The worker is killed mid-dispatch, a new worker resumes from the last checkpoint, and the effects the dead worker had already completed are reused rather than repeated - ending with zero duplicate effects at $0.0000."
       width="100%">
</p>

> **This animation is a playback, not a mock-up.** It is rendered directly from
> a real run's event log by [`scripts/render_animation.py`](scripts/render_animation.py):
> the lifecycle strip is driven by actual `PHASE_ENTERED` events, the log pane
> shows real sequence numbers, and the counters are read off the real
> `RunResult`. The script refuses to render if the run reports a single
> duplicate effect — so the animation cannot claim something the runtime does
> not do. Regenerate it with `python scripts/render_animation.py`.

Watch for the moment at `ACTION_DISPATCHED save_note`: the dispatch is on disk,
the effect is not, and the process dies exactly there. That is the state every
agent runtime has to survive, and the two gold `EFFECT_REUSED` lines afterwards
are how this one does.

```
model proposes  →  VALIDATE  →  AUTHORIZE  →  DISPATCH  →  OBSERVE
                                                              ↓
              COMMIT  ←  RECONCILE  ←──────────────────────────┘
```

Nothing a model emits mutates canonical state directly. `Proposal` and
`Action` are separate types, and only the runtime can mint an `Action`.

---

## Try it in 30 seconds

No API key, no Docker, no database to start. Requires Python 3.11+.

```bash
pip install -e .
python -m forge.cli demo
```

That command starts a run, **kills the worker mid-flight**, restarts it,
resumes from the last checkpoint, and proves no external effect happened
twice:

```
  [1/4] starting a run that will be killed mid-flight ...
        worker died: injected worker crash at step 3
  [2/4] log holds 57 events; checkpoint at step 2
  [3/4] restarting a fresh worker and resuming ...
  [4/4] status=COMPLETED steps=5

        effects performed   : 3
        effects reused      : 2  (suppressed on resume)
        duplicate effects   : 0
        unique idempotency  : 3 of 3
        cost                : $0.0000
```

Then inspect exactly what happened:

```bash
forge trace <run_id> --db .forge/demo.db
forge policy-show
forge bench --trials 3
```

---

## Why it runs at $0.00

The cost ceiling is not a note-to-self about being frugal. It is enforced by
the policy engine at dispatch time:

```yaml
budget:
  usd_ceiling: 0.00        # hard stop, not a warning
capabilities:
  PAID_INFERENCE:
    granted: false         # any adapter with price_per_1k > 0 is denied
    requires_approval: true
```

Any provider whose `pricing` is non-zero requires the `PAID_INFERENCE`
capability, which is not granted. Spending money requires a deliberate,
logged, human-approved policy change. A free-tier `429` therefore degrades
into a **policy-driven fallback with a recorded decision**, rather than an
outage.

Three inference tiers, all free:

| Tier | Provider | Used for | Share of cycles |
|---|---|---|---|
| 0 | `MockProvider` | every test, benchmark and replay | ~85% |
| 1 | `OllamaProvider` (local) | real inference, real failure modes | ~13% |
| 2 | free cloud tiers | the demo recording, frontier comparison | ~2% |

`MockProvider` was written *first*, before any agent logic. Making the first
provider not a provider at all forces the gateway abstraction to be honest.

---

## What it actually does

| Plane | Module | Responsibility |
|---|---|---|
| Execution | `forge/runtime/` | the 15-phase lifecycle, retries, reconciliation, loop bounds |
| State | `forge/state/` | append-only event log, checkpoints, projections |
| Trust | `forge/security/` | capabilities, single-use permits, budgets, deny-by-default policy |
| Context | `forge/context/` | bounded, priority-ordered, budget-trimmed views |
| LLM | `forge/llm/` | provider-neutral gateway, cost ledger, fallback routing |
| Tools | `forge/tools/` | typed contracts, side-effect classes, compensators |
| Telemetry | `forge/telemetry/` | OTel-compatible spans, Prometheus metrics, redaction |
| Evaluation | `forge/evaluation/` | fault injection, benchmark runner, replay + diff |

### The safety argument, in three properties

**1. Nothing dispatches without an authorization decision.** The lifecycle is
an explicit transition table (`forge/runtime/machine.py`). `AUTHORIZE` is the
only phase with an edge into `DISPATCH`. This is not a convention -
`tests/unit/test_machine_invariants.py` searches the graph exhaustively and
fails if an edit ever adds a shortcut.

**2. An effect happens at most once.** The idempotency key is derived from
run id, tool, canonical arguments and attempt group. Recording an effect *is*
the claim on that key - a single durable append against a `UNIQUE` index. So
after any crash the effect is either in the log (and resume reuses it) or it
is not (and resume performs it). Never both.

**3. Crash-resume is not a second code path.** Canonical state is defined as
the fold of the event log. The live runtime and the resume path both call
`project()`, so a resumed run cannot drift from one that never crashed.

---

## The benchmark

Resilience claims are worthless without reproduction. Every trial is seeded
and runs on `MockProvider`, so the whole suite is free and offline:

```bash
forge bench --trials 3        # writes reports/failure_injection.{json,md}
```

Current results (`v0.1.0`, seed 1729, 63 trials, **total spend $0.00**):

| Injected failure | Task success | Recovered | Contained | Dup effects |
|---|---:|---:|---:|---:|
| `none` (control) | 100% | 100% | 100% | 0 |
| `worker_crash` | 100% | 100% | 100% | 0 |
| `llm_timeout` | 100% | 100% | 100% | 0 |
| `malformed_output` | 100% | 100% | 100% | 0 |
| `tool_timeout` | 100% | 100% | 100% | 0 |
| `policy_denial` | 100% | 100% | 100% | 0 |
| `repeated_action_loop` | 33% | 33% | **100%** | 0 |

`repeated_action_loop` is the interesting row. The correct response to an
unbreakable loop is to *stop*, so it is contained but never completed.
Reporting only "recovered" would penalise the runtime for behaving correctly;
reporting only "contained" would flatter it. Both are shown.

Three rows started lower and were fixed during development, which is what the
benchmark is for:

- `llm_timeout` began at **0%**. A transient provider failure ended the run
  instead of being retried, contradicting the retry taxonomy the runtime
  itself declares. Fixed by separating transient model retries from proposal
  repairs, each with its own budget.
- `tool_timeout` and `policy_denial` began at **33%** - and those were bugs in
  the *injector*, not the runtime. Both faults were being raised at points
  their real counterparts never reach, bypassing effect recording and the
  denial path. Fault placement is a correctness question: a fault must enter
  where its real counterpart would, or the benchmark measures the harness.

A separate bug surfaced from exercising `forge replay`: `PROPOSAL_RECEIVED`
recorded `kind`, `tool` and `arguments` but not `answer`, so replay could not
reconstruct a final ANSWER turn and diverged on the last step of every run.
The log has to be sufficient to rebuild what it describes.

---

## Development

```bash
pip install -e ".[dev]"
pytest                       # 111 tests, ~20s, no network
ruff check forge tests       # clean
mypy forge                   # strict, clean across 40 modules
pytest -m recovery           # crash/resume correctness only
pytest -m adversarial        # attempts to defeat the trust plane
```

The recovery suite includes `test_hard_process_kill_then_resume`, which kills
a real OS process with `os._exit()` - no exception propagation, no `finally`,
no flush - and then resumes from whatever reached the disk. A "crash" the
runtime can catch and clean up proves nothing.

### Regenerating the animation

```bash
pip install -e ".[media]"
python scripts/render_animation.py    # -> docs/assets/forge-demo.gif + forge-panel.png
```

The script executes the crash/resume scenario for real, reads back the event
log, and renders it. It exits non-zero rather than producing a GIF if the run
reports any duplicate effect, so a regression cannot quietly ship alongside an
animation that still claims success.

### Using a local model

```bash
ollama serve
forge doctor                                  # check what's reachable
forge run "Summarise the corpus" --provider ollama --model qwen3:8b
```

Structured output is enforced provider-side via Ollama's JSON-schema `format`
parameter, which is considerably more reliable on an 8B model than asking
politely and repairing the wreckage.

---

## Scope

This is **v0.1**, covering milestones M0-M5 of the build plan: typed core,
durable state, trust plane, observability, replay, and the failure-injection
benchmark. It satisfies 8 of the 11 acceptance criteria in the specification.

Deliberately **not** built yet, in the order they should come:

- **DAG engine** (M6) - parallel branches, fan-in, compensation nodes.
  Loop detection and compensation exist; the scheduler does not.
- **Governed memory** (M7) - pgvector store, admission policy, TTL,
  supersession. The context compiler is bounded and priority-ordered but reads
  only from run state, not long-term memory.
- **Sandbox and multi-agent contracts** (M8) - the `Proposal` type reserves
  `DELEGATE`, but nothing consumes it.
- **Postgres backend** - the `EventStore` protocol is five methods wide
  specifically so this stays a small delta. See ADR-0004.

See [docs/adr/](docs/adr/) for the reasoning behind the load-bearing choices.

## Licence

Apache-2.0.
