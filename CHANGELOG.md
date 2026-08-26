# Changelog

All notable changes to this project are documented here.
This project follows [Semantic Versioning](https://semver.org/).

Anything exported from the top-level `forge` package is public API and covered
by the version guarantee. Anything reached through a submodule path is
internal and may change in a minor release.

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
