# Deploying FORGE

What you need to know to run this somewhere real, including the parts that
are not ready yet.

## Read this first: the current ceiling

FORGE ships the **SQLite** event store. SQLite allows many readers and one
writer, so:

- **Run one replica per database.** Two API containers against the same file
  will serialise on writes and, under load, exhaust `busy_timeout`.
- Scale vertically (`FORGE_MAX_CONCURRENT_RUNS`) rather than horizontally, or
  shard by giving each replica its own database and routing runs to it.
- Horizontal scale needs the Postgres backend, which is designed for but not
  built. The `EventStore` protocol is deliberately narrow so it stays a small
  delta — see [ADR-0004](adr/0004-sqlite-default-postgres-optional.md).

The lease and supervisor machinery is already multi-replica correct, so when
Postgres lands nothing above it has to change.

## Quick start

```bash
docker build -t forge:0.3.0 .

docker run -d --name forge \
  -p 8080:8080 \
  -v forge-data:/data \
  -e FORGE_API_KEYS="prod:$(openssl rand -hex 32)" \
  -e FORGE_PROVIDER=openai \
  -e FORGE_MODEL=gpt-4o-mini \
  -e FORGE_API_KEY_ENV=OPENAI_API_KEY \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  forge:0.3.0
```

`/data` **must** be durable storage. Losing it loses every in-flight run's
ability to resume, which is the one thing this system exists to guarantee.

## Configuration

Precedence: built-in defaults → `forge.toml` → `FORGE_*` environment.

| Variable | Purpose |
|---|---|
| `FORGE_API_KEYS` | `label:key` pairs, comma-separated. **Required** unless auth is disabled. |
| `FORGE_DATABASE_URL` | `sqlite:////data/forge.db` |
| `FORGE_PROVIDER` | `openai` \| `ollama` \| `mock` |
| `FORGE_MODEL` | Model id |
| `FORGE_BASE_URL` | For any OpenAI-compatible endpoint |
| `FORGE_API_KEY_ENV` | Name of the variable holding the provider key — never the key |
| `FORGE_MAX_USD` | Per-run spend ceiling |
| `FORGE_MAX_STEPS`, `FORGE_MAX_TOKENS` | Per-run resource ceilings |
| `FORGE_POLICY_BUNDLE` | Path to a policy YAML |
| `FORGE_MAX_CONCURRENT_RUNS` | In-process concurrency gate |
| `FORGE_OTEL` | `true` to mirror spans to an installed OTel SDK |

Provider *chains* belong in `forge.toml`, not the environment — an ordered list
of dictionaries encoded into an environment variable is a format nobody should
debug at 3am.

```toml
[[providers]]                  # tried in order; failover is automatic
kind = "openai"
model = "gpt-4o-mini"
api_key_env = "OPENAI_API_KEY"
input_per_1k = 0.00015
output_per_1k = 0.0006

[[providers]]                  # fallback when the first is unaffordable or down
kind = "ollama"
model = "qwen3:8b"
```

## Probes

| Endpoint | Use | Semantics |
|---|---|---|
| `/livez` | liveness | Process is up. Touches nothing else, so a slow dependency cannot get a healthy container killed. |
| `/readyz` | readiness | Can take traffic now. Returns `503` while draining, and if auth is required but unconfigured. |
| `/healthz` | dashboards | Detail: providers, in-flight count, supervisor stats. |
| `/metrics` | Prometheus | Text exposition. Unauthenticated by design — scrape from inside the network. |

Kubernetes:

```yaml
livenessProbe:
  httpGet: { path: /livez, port: 8080 }
  periodSeconds: 30
readinessProbe:
  httpGet: { path: /readyz, port: 8080 }
  periodSeconds: 5
terminationGracePeriodSeconds: 40   # must exceed the drain timeout below
```

## Shutdown and recovery

On SIGTERM the service stops accepting work, drains in-flight runs for up to
25 seconds, then cancels what remains. **Cancelled runs are not lost** — each
holds a lease that expires, and a supervisor reclaims and resumes it from the
last checkpoint.

Set `terminationGracePeriodSeconds` above the drain window so the orchestrator
does not `SIGKILL` mid-drain. Even if it does, recovery still works — draining
is an optimisation, not the safety mechanism.

**Recovery is automatic.** On startup and every 30 seconds, the supervisor
sweeps for runs with no terminal event and no live lease, and resumes them.
This includes runs abandoned by a deployment that no longer exists.

A run that fails to recover `max_recovery_attempts` times is left alone and
reported in `/healthz` under `supervisor.abandoned`, rather than retried
forever in a loop that looks like progress. **Alert on that field.**

## Retention

The event log is append-only and grows without bound. Prune on a schedule:

```bash
forge prune --older-than-days 30
```

Whole finished runs only; unfinished runs are retained by default because they
may still be recoverable. Eligibility is by newest event, so a run created
months ago but resumed yesterday is not truncated.

## What to alert on

| Signal | Why |
|---|---|
| `forge_duplicate_effects_total > 0` | An external effect happened twice. This should be structurally impossible; investigate immediately. |
| `supervisor.abandoned` non-empty | Runs the supervisor gave up recovering. |
| `/readyz` failing while `/livez` passes | Misconfiguration or a dead provider, not a dead process. |
| `forge_policy_denials_total` rising | Either an agent misbehaving or a policy that is too tight. |
| Disk usage on `/data` | See retention above. |

## Security posture — state of play

**In place:** bearer-token auth with constant-time comparison, per-principal
rate limiting, request size limits, deny-by-default tool authorization,
single-use permits bound to an action hash, secret redaction before anything is
persisted or logged, non-root container, provider keys read from the
environment by name.

**Not in place, and you should plan around it:**

- **No sandbox.** A tool executes in the service process. Do not register a
  tool that runs arbitrary code until the sandbox milestone lands. The
  shipped tools are safe (the calculator evaluates an AST allow-list, not
  `eval`), but a tool *you* add is only as safe as you make it.
- **No multi-tenancy.** One policy bundle, one budget scope. API keys identify
  a caller for logging and rate limiting; they do not isolate data.
- **No cross-run budget.** Ceilings are per run. A thousand runs each just
  under the ceiling is not caught. Enforce a fleet budget upstream.
- **In-process rate limiting.** It bounds one replica, not a fleet. Real quota
  belongs in the gateway.

## Backups

Back up `/data`. With WAL journalling, copy `forge.db`, `forge.db-wal` and
`forge.db-shm` together, or use `sqlite3 forge.db ".backup out.db"` for a
consistent snapshot without stopping the service.

Restoring an old backup replays history the supervisor may act on: runs that
were unfinished at snapshot time will be resumed. That is usually what you
want, and is worth being deliberate about.
