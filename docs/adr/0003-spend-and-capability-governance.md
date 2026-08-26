# ADR-0003: Spend is authorized, not reported

- **Status:** accepted
- **Date:** 2026-08-25 (revised 2026-08-26)

## Context

An agent decides at run time how much work to do. Unlike a request/response
service, its cost is not bounded by its input: a long-horizon run can loop,
re-plan, retry, and quietly spend two orders of magnitude more than the median
before anyone notices.

The common answer is a dashboard and an alert. That is a bill arriving slightly
faster. By the time a threshold fires, the money is gone, and the run that spent
it has usually finished.

Separately, the runtime already needed capability-based authorization at
dispatch time and per-run resource budgets. Cost is a resource like any other.

## Decision

Express spend control through the authorization machinery that already exists,
and check it **before** the request leaves the process.

- Every provider declares `Pricing`. It is a property of the adapter, not a
  configuration flag someone can forget to set.
- `LLMGateway` projects a call's cost from the request's own token ceiling — an
  upper bound, not a hopeful estimate — and compares it to the run's remaining
  budget.
- A provider that does not fit is **skipped**, and the gateway moves down the
  failover chain, recording a `RouteAttempt` explaining the skip.
- `PolicyEngine.authorize_inference` performs the same check as an ordinary
  capability decision, so it lands in the event log next to every other
  decision.
- When nothing in the chain fits, the run fails with `BudgetExhausted` rather
  than silently truncating.

Budgets live in the versioned policy bundle, so a ceiling is a reviewable
config change with an audit trail, not an environment variable someone
overrode on a laptop.

## Consequences

**Good.** A run cannot discover it is over budget by having already gone over.
`test_unaffordable_provider_is_never_called` asserts the provider's `complete()`
is never even invoked.

**Good, and initially unexpected.** Budget pressure degrades into *routing*
rather than failure. A run that cannot afford the frontier model falls back to
a cheaper or self-hosted one and keeps going, with the whole decision chain
visible in the trace. The constraint produced a resilience feature.

**Good.** It demos well and it explains well: raise the ceiling, watch routing
change; lower it, watch failover.

**Costs.** Providers must declare pricing honestly. This is authorization, not
sandboxing — it trusts the adapter. First-party adapters are reviewed; a
third-party adapter would need to be.

**Costs.** The ceiling is per-run. There is no cross-run or per-tenant budget
yet, so a thousand runs each just under the ceiling is not caught. That is the
obvious next gap and it should be closed before this runs a fleet.

**Costs.** Projecting on `max_tokens` over-estimates most calls, so the gate is
conservative: it will occasionally fail over when the actual call would have
fit. Preferred over the alternative error.

## Alternatives considered

- **Post-hoc reporting.** Tells you what you already spent. A budget that
  reports rather than refuses is a bill.
- **An environment variable.** Not versioned, not attached to the run record,
  not reviewable, and trivially overridden.
- **A hard-coded ceiling.** Would make the runtime unusable for the workloads
  that most need governance — the expensive ones.
