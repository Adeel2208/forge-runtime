# Changelog

All notable changes to this project are documented here.
This project follows [Semantic Versioning](https://semver.org/).

Anything exported from the top-level `forge` package is public API and covered
by the version guarantee. Anything reached through a submodule path is
internal and may change in a minor release.

## [0.6.0] - 2026-08-27

### Added

- **Interactive session** — `forge` with no arguments. Describe a task in
  plain language, watch each tool call as it happens, then `/diff`, `/accept`
  or `/undo`. Slash commands for `/status`, `/policy`, `/trace` and
  `/history`. Built on stdlib ANSI, so the dependency list stays at four.
- **The approval gate finally reaches a human.** The runtime could always
  return `REQUIRE_APPROVAL`, but every entry point until now either
  auto-approved or refused. A session is where a person is actually present,
  so that is where it is answered — defaulting to **no**, because an operator
  who hits return without reading should get the safe outcome.
- `/accept` merges the run's branch; `/undo` deletes it. Nothing reaches your
  branch unless you say so, and quitting with unmerged work tells you where
  it is rather than leaving a branch you never learn about.

### Changed

- The sandbox is selected once per session rather than per run, so the banner
  and `/policy` report the tier that will actually apply. A capability granted
  in YAML but blocked by insufficient isolation now displays as **BLOCKED**
  with the reason, instead of reading as granted — the same class of
  confident-but-wrong display as a CLI that answers without a model.

### Fixed

- UI glyphs are ASCII. `✓` and `─` are unencodable in cp1252 and would have
  raised `UnicodeEncodeError` part-way through rendering on a default Windows
  console.
- The session prompt reads input on a worker thread; a blocking `input()`
  held the event loop for as long as the user was thinking.

## [0.5.0] - 2026-08-27

### Added

- **Sandbox** (`forge.sandbox`) with three declared tiers — `NONE`,
  `CONFINED`, `CONTAINER` — where each states what it enforces *and what it
  does not*. `CONFINED` gives no shell, a scrubbed environment, a confined
  working directory, wall-clock and output ceilings, OS-enforced resource caps
  (Windows Job Objects, Unix rlimits) and guaranteed process-tree kill.
  `CONTAINER` adds filesystem, network, PID and user isolation.
- **Policy requires a minimum tier.** `requires_isolation` on a capability
  grant is compared against what the machine actually provides, so `SHELL`
  stays denied where there is no container runtime — automatically, with a
  reason naming the tier it wanted. `select_sandbox` refuses rather than
  silently degrading.
- **Environment scrubbing** built on an allow-list, so a command never sees a
  token it could leak, log or commit. Even an explicitly passed-through name
  is dropped if it matches a credential pattern.
- Every command the coding agent runs — `run_tests` included — now goes
  through the sandbox. There is one execution call site to audit.

### Notes

`CONFINED` bounds accidents and runaway resource use. It does **not** contain
a program actively trying to escape; the test suite asserts this explicitly
rather than leaving it implied, including a test verifying that `CONFINED`
genuinely does not isolate the filesystem.

## [0.4.0] - 2026-08-27

### Added

- **Coding agent** (`forge code`). A local-first agent that edits code under
  runtime control: every run on its own git branch, every step a commit, every
  edit compensated by a git restore. Path confinement refuses `..`, absolute
  escapes and symlinks pointing outward; `.git/`, `.env` and key files are
  invisible to reads. Shell execution is declared and ungranted.
- **`forge init`** scaffolds a working project — `forge.toml`, `tools.py`,
  `policy.yaml`, `cases/` — rather than leaving a blank directory.
- **`tools_module` config**, so a deployment points at its own `ToolRegistry`
  instead of the bundled examples.
- `forge code review` / `forge code discard`; `forge prune`; `forge serve`.

### Fixed

Five bugs found by installing the wheel into a clean environment and using it
as a stranger would, and four more by running `qwen3:8b` against a real repo:

- **`forge run` returned a canned answer** when no model was configured — it
  looked like it worked. It now refuses and says how to configure one.
- **`forge doctor` reported packaged defaults**, not the project's actual
  tools and policy.
- **Project-local `tools.py` was unimportable** from a console-script entry
  point, which does not put the working directory on `sys.path`.
- **A failed run reported no reason**: `error` was only set on exception paths.
- **`ProviderConfig.timeout_s` was never plumbed through**, and the 512-token
  proposal ceiling truncated reasoning models into empty completions.
- **`git rev-parse --is-inside-work-tree` is true for any descendant**, so
  running in a subdirectory would have branched and committed the *parent*
  repository. Now requires the repo root and names it in the refusal.
- **`edit_file` failed on the line-number gutter** that `read_file` displays
  and models copy back. Stripped and retried, and the normalisation recorded.
- **The gutter regex ate line breaks** (`\s?` matches `\n`), silently
  collapsing blank lines so the "fix" did not work.
- **`.forge/` was committed into the user's repository** by `git add -A`.
- **Re-applied edits duplicated code**: each insertion succeeded, producing
  three copies of a function and a green test suite.

### Changed

- `InProcessTarget` now evaluates the *project's* configuration rather than
  library defaults, and reports the model in the target version — a verdict is
  (case-set version × target version), and for an agent the model is the target.
- Removed the `postgres` extra: it installed a driver for a backend that does
  not exist.

## [0.3.0] - 2026-08-26

The release that closes the gap between "the runtime survives a crash" and
"the service survives a crash".

### Added

- **Run supervisor** (`forge.runtime.supervisor`). Abandoned runs are found and
  resumed automatically — on startup and on an interval — so a killed worker no
  longer strands a run until an operator notices. Recovery re-enters the normal
  resume path, so it inherits the same exactly-once effect guarantee.
- **Ownership leases** (`forge.state.leases`). A run is owned by exactly one
  worker; claiming is a single atomic conditional write, so concurrent
  supervisors cannot both recover the same run. A dead worker's lease simply
  expires — no shutdown hook is required for correctness.
- **API authentication** (`forge.api.security`). Bearer-token / `X-API-Key`
  auth with constant-time comparison, plus per-principal rate limiting.
  A deployment that requires auth but configures no keys fails closed and
  reports `503` on `/readyz` rather than silently serving open.
- **Graceful shutdown.** SIGTERM stops accepting work, drains in-flight runs,
  and leaves anything unfinished to be recovered.
- **`/readyz`** separate from `/livez`, so a slow dependency cannot get a
  healthy container killed.
- **Structured logging** (`forge.telemetry.logging`). One JSON object per line,
  keyword fields, redaction applied to every value. Configures only the
  `forge.*` logger, never the root.
- **Event-log retention** (`EventStore.prune`). Whole finished runs older than a
  cutoff; unfinished runs retained by default. Eligibility is by newest event,
  so a recently resumed old run is not truncated.
- **`py.typed`** — downstream consumers now get types.
- Test coverage for the HTTP service (16 tests) and the supervisor (11 tests),
  both previously untested.

### Changed

- `Harness` (the deployment facade) is now **`Forge`**. `forge.eval.Harness` is
  the evaluation harness, and having two things called "harness" was worse than
  the rename. **Breaking.**
- SQLite connections set `busy_timeout=5000`, so a second process waits for the
  writer rather than failing immediately.
- `EventStore` gained `unfinished_runs()` and `prune()`. **Breaking** for
  third-party implementations of the protocol.

### Fixed

- FastAPI dependency resolution: a locally-defined `Annotated` alias is invisible
  to FastAPI under `from __future__ import annotations`, which silently demoted
  the authentication parameter to a query field and returned `422`.

## [0.2.0] - 2026-08-26

### Added

- **Evaluation harness** (`forge.eval`) — a generic runner over versioned case
  sets. Cases are YAML data with stable ids; targets are pluggable adapters
  (in-process, HTTP, CLI, callable); graders are selected by the case data and
  swappable without touching the runner.
- Seven-value `Outcome` vocabulary so infrastructure noise never masquerades as
  a verdict on the target. Retries are gated to infrastructure only.
- Structured results as JSONL plus a manifest, carrying case-set version and
  target version. `compare` refuses to diff across case-set versions.
- Negative fixtures (`tests/eval/test_harness_meta.py`): the harness is tested
  against deliberately broken targets it must fail.
- `ForgeConfig` — TOML + `FORGE_*` environment configuration.
- `OpenAICompatProvider`, covering OpenAI, Groq, Together, Fireworks,
  OpenRouter, Azure, vLLM and LM Studio.
- FastAPI service, Prometheus metrics endpoint.

### Changed

- Spend governance reframed: per-run ceilings checked at `AUTHORIZE` with
  budget-aware provider failover, replacing the previous zero-cost framing.
  `PolicyBundle.zero_cost()` is now `PolicyBundle.baseline()`. **Breaking.**
- Policy bundle `zero_cost.yaml` renamed to `default.yaml`.

## [0.1.0] - 2026-08-25

Initial release: typed core, durable event log, checkpoints and crash-resume
with exactly-once effects, capability-based trust plane with single-use permits,
OpenTelemetry-compatible tracing, deterministic replay, and a failure-injection
benchmark across seven fault classes.
