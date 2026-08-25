"""FORGE - a durable, policy-aware execution runtime for long-horizon AI agents.

The runtime's contract with the model is deliberately narrow:

    the model *proposes*; the runtime validates, authorizes, dispatches,
    observes, reconciles and commits.

Nothing a model emits mutates canonical state directly. See
`forge.runtime.machine` for the lifecycle that enforces this.

Run an agent::

    from forge import Forge

    async with Forge.from_config() as forge:
        result = await forge.run("Summarise the Q3 incident reports")
        print(result.answer)

Evaluate one::

    from forge.eval import CaseSet, Harness, InProcessTarget

    results = await Harness(CaseSet.load("cases/"), InProcessTarget()).run()
    results.write("reports/latest")

`Forge` runs agents; `forge.eval.Harness` runs *cases against targets*. They
are deliberately different objects: the harness must not know what a FORGE run
is, only what a `Target` and a `Grade` are.

Everything exported here is public API and follows semantic versioning.
Anything reached through a submodule path is internal and may change.
"""

from __future__ import annotations

__version__ = "0.2.0"

from forge.config import BudgetConfig, ForgeConfig, ProviderConfig, TelemetryConfig
from forge.core.contracts import Effect, Proposal, TaskSpec
from forge.core.enums import Decision, Phase, RunStatus, SideEffect
from forge.errors import (
    BudgetExhausted,
    ForgeError,
    PolicyDenied,
    ProviderUnavailable,
    UnrecoverableError,
)
from forge.deployment import Forge
from forge.runtime.loop import RunResult
from forge.tools.registry import ToolRegistry, ToolSpec

__all__ = [
    "__version__",
    # entry points
    "Forge",
    # configuration
    "ForgeConfig",
    "ProviderConfig",
    "BudgetConfig",
    "TelemetryConfig",
    # task and result contracts
    "TaskSpec",
    "RunResult",
    "Proposal",
    "Effect",
    # vocabularies
    "RunStatus",
    "Phase",
    "Decision",
    "SideEffect",
    # tools
    "ToolRegistry",
    "ToolSpec",
    # errors
    "ForgeError",
    "PolicyDenied",
    "BudgetExhausted",
    "ProviderUnavailable",
    "UnrecoverableError",
]
