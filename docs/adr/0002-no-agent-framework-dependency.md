# ADR-0002: No agent-framework dependency

- **Status:** accepted
- **Date:** 2026-08-25

## Context

LangChain, LlamaIndex, CrewAI and AutoGen all offer agent loops, tool
abstractions and provider adapters. Adopting one would remove perhaps a
thousand lines from this repository.

## Decision

FORGE depends on **pydantic, httpx, typer and PyYAML**. Nothing else is
required. No agent framework, at any layer, including in tests and examples.

## Rationale

The claim this project makes is that the *runtime* - lifecycle, authorization,
durability, reconciliation - is the contribution. A framework dependency
silently retracts that claim: a reader can no longer tell which properties are
ours and which are inherited, and neither can we.

There is a concrete technical reason too. Every framework we surveyed treats
the agent loop as the extension point and the tool call as a leaf. FORGE needs
the opposite: the tool call is where authorization, idempotency and effect
reconciliation live, and the loop is the part we most need to constrain. We
would be fighting the framework at precisely the layer that matters.

The safety properties in ADR-0001 and the permit design also require control
over the exact boundary between proposal and dispatch. That boundary is
usually a framework's private internals.

## Consequences

**Good.** The dependency surface is four packages, all of which would be
present anyway. Install is fast, the supply-chain surface is small, and
`pip install -e .` works offline on a clean Python 3.11.

**Good.** Every property in the README is demonstrably ours.

**Costs.** We write our own provider adapters. In practice this was ~120 lines
for Ollama, because the gateway abstraction is narrow by design.

**Costs.** We forgo the retrieval and document-loading ecosystem. Acceptable:
those are application concerns that belong *above* a runtime, and a runtime
that hard-depends on a document loader has a layering problem.
