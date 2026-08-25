# ADR-0005: `Proposal` and `Action` are different types

- **Status:** accepted
- **Date:** 2026-08-25

## Context

The central safety claim of FORGE is that a model's output never becomes a
side effect without passing through validation and authorization. Most agent
implementations express this as a sequence of function calls - parse, check,
then execute - which means the guarantee survives exactly as long as nobody
adds an early return or a convenience path.

That is a code-review guarantee. Code-review guarantees decay.

## Decision

Model the boundary in the type system.

- `Proposal` is what the model said. It has `kind`, `tool`, `arguments`,
  `answer`, and a redacted `rationale_summary`. It carries no permit, no
  idempotency key, and no side-effect class.
- `Action` is what the runtime authorized. It *requires* `permit_id`,
  `idempotency_key` and `side_effect` at construction.
- `_dispatch()` accepts an `Action`. It has no overload accepting a
  `Proposal`.

Constructing an `Action` requires values that only `_authorize()` can produce:
a permit id from the `PermitBook`, and a side-effect class from the registered
`ToolSpec`. There is no path from raw model output to dispatch that does not
go through authorization, because the argument list cannot be filled in.

Both types are frozen with `extra="forbid"`.

## Consequences

**Good.** "The model's output was executed directly" is not a thing a reviewer
has to watch for. It fails at construction.

**Good.** The permit is bound to `action_hash`, so a permit issued for one
action cannot be redeemed for another (`test_a_permit_cannot_be_escalated_to_another_action`).
Redemption is single-use and destructive, so a retry that forgets to
re-authorize fails loudly rather than quietly re-executing a write.

**Good.** The log distinguishes `PROPOSAL_RECEIVED` from `ACTION_DISPATCHED`,
which makes "what did the model want vs what was it allowed to do" directly
answerable - including for denied proposals, which have the former and never
the latter.

**Costs.** Two similar-looking types, and a mapping step between them. This is
real friction when adding a proposal kind. It is the friction working.

**Costs.** The type system enforces *structure*, not policy. Nothing stops
`_authorize()` from being wrong about whether to issue a permit. That is what
`tests/adversarial/` is for. Types close the accidental holes; tests cover the
deliberate ones.
