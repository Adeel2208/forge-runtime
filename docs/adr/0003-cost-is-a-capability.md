# ADR-0003: Cost is a capability

- **Status:** accepted
- **Date:** 2026-08-25

## Context

This project had a hard constraint: it must cost **$0.00** to build, test and
demonstrate. The naive response is discipline - remember to use free models,
watch the dashboard, do not leave a benchmark running overnight.

Discipline is not an architecture. It fails silently, at 3am, in a loop.

Separately, the specification already requires resource budgets (§13), cost
accounting (§15) and capability-based authorization at dispatch time (§12).

## Decision

Express the budget constraint *through* the authorization machinery that
already had to exist:

- Every provider declares `Pricing`. `pricing.is_free` is a property of the
  adapter, not a configuration flag.
- The policy bundle sets `usd_ceiling: 0.00`.
- Reaching a non-free provider requires the `PAID_INFERENCE` capability, which
  is declared but **not granted**, and additionally `requires_approval`.
- `LLMGateway` checks the ceiling before each attempt and moves down the
  fallback chain, recording a `RouteAttempt` for every skip.

Spending money is therefore a deliberate, logged, human-approved policy change
rather than an accident.

## Consequences

**Good.** The bill is structurally zero. `test_zero_ceiling_blocks_a_paid_provider`
asserts a paid provider's `complete()` is never even called.

**Good, and unexpected.** A free-tier `429` stops being an outage and becomes
a policy-driven fallback with a recorded decision and a trace span - which is
exactly the resilience behaviour §13 and §16 ask us to demonstrate. The
constraint produced a feature.

**Good.** It demos well. Grant `PAID_INFERENCE` live, watch routing change;
revoke it, watch the runtime fall back to a local model and keep going, with
the whole decision chain visible.

**Costs.** Providers must declare pricing honestly. A provider that lies about
being free defeats the gate - this is authorization, not sandboxing, and it
trusts the adapter. Adapters are first-party code; third-party adapters would
need review.

**Costs.** The ceiling is per-run. A thousand runs at $0.00 is still $0.00, but
under a raised ceiling there is no global cross-run budget yet. Noted as a gap.

## Alternatives considered

- **Environment variable `FORGE_MAX_USD`.** Not auditable, not versioned, not
  attached to the run record, and trivially overridden.
- **Post-hoc cost reporting.** Tells you what you already spent. A budget that
  reports rather than refuses is a bill.
