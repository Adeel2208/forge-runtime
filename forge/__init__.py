"""FORGE - a durable, policy-aware execution runtime for long-horizon AI agents.

The runtime's contract with the model is deliberately narrow:

    the model *proposes*; the runtime validates, authorizes, dispatches,
    observes, reconciles and commits.

Nothing a model emits mutates canonical state directly. See
`forge.runtime.machine` for the lifecycle that enforces this.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
