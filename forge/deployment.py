"""The public entry point.

Everything below this module is assembled machinery; this is the surface a
user touches:

    async with Forge.from_config() as forge:
        result = await forge.run("Summarise the Q3 incident reports")
        print(result.answer)

`AgentRuntime` still takes eight collaborators by keyword, and that is correct
for a runtime whose whole point is that its parts are swappable and inspectable.
But wiring eight collaborators is not a thing an application should do to say
"run this task". The deployment owns assembly, lifetime and concurrency; the
runtime owns execution.

One `Forge` maps to one deployment: one event store, one policy bundle, one
provider chain. It is safe to share across concurrent runs - each run gets a
freshly built `AgentRuntime`, because runtimes carry per-run state (permits,
loop detection, ledger) that must never be shared between runs.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from contextlib import suppress
from importlib import import_module
from pathlib import Path
from types import TracebackType
from typing import Any

from forge.config import ForgeConfig, ProviderConfig
from forge.context.compiler import ContextCompiler
from forge.core.contracts import TaskSpec
from forge.core.events import Event
from forge.errors import UnrecoverableError
from forge.llm.base import LLMProvider, Pricing
from forge.llm.gateway import CostLedger, LLMGateway
from forge.llm.mock import MockProvider
from forge.llm.ollama import OllamaProvider
from forge.llm.openai_compat import OpenAICompatProvider
from forge.runtime.loop import AgentRuntime, RunResult, RuntimeConfig
from forge.security.policy import PolicyBundle, PolicyEngine
from forge.state.sqlite_store import SQLiteEventStore
from forge.state.store import EventStore
from forge.telemetry.metrics import Metrics
from forge.telemetry.tracer import Tracer
from forge.tools.registry import ToolRegistry

__all__ = ["Forge"]

PACKAGED_POLICY = Path(__file__).parent / "security" / "policies" / "default.yaml"


class Forge:
    """A configured FORGE deployment."""

    def __init__(
        self,
        *,
        config: ForgeConfig | None = None,
        store: EventStore | None = None,
        registry: ToolRegistry | None = None,
        providers: Sequence[LLMProvider] | None = None,
        policy: PolicyBundle | None = None,
        available_isolation: str = "confined",
        approval: Any = None,
    ) -> None:
        self.config = config or ForgeConfig()
        self.available_isolation = available_isolation
        self.approval = approval
        """Answers REQUIRE_APPROVAL. Without one the runtime refuses, which is
        the safe default for an unattended deployment."""
        """What this machine can actually enforce. Capabilities in the policy
        bundle that require more remain denied - see forge.sandbox."""
        self.metrics = Metrics()
        self._store = store or self._build_store(self.config)
        self._registry = registry or self._build_registry(self.config)
        self._providers = list(providers) if providers is not None else None
        self._policy_bundle = policy
        self._opened = False
        self._gate = asyncio.Semaphore(self.config.max_concurrent_runs)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_config(
        cls, path: str | Path | None = None, **overrides: Any
    ) -> Forge:
        """Load `forge.toml` + `FORGE_*` and build a harness from it."""
        return cls(config=ForgeConfig.load(path), **overrides)

    @classmethod
    def for_testing(cls, script: Sequence[dict[str, Any]], **overrides: Any) -> Forge:
        """An in-memory harness backed by a scripted provider.

        The supported way to test application code that calls FORGE, without
        a model, a network or a database file.
        """
        config = ForgeConfig(database_url="sqlite:///:memory:")
        return cls(config=config, providers=[MockProvider(script)], **overrides)

    @staticmethod
    def _build_store(config: ForgeConfig) -> EventStore:
        if not config.is_sqlite:
            raise UnrecoverableError(
                f"unsupported database_url {config.database_url!r}; "
                "this build ships the SQLite backend only "
                "(see docs/adr/0004-sqlite-default-postgres-optional.md)",
            )
        return SQLiteEventStore(config.sqlite_path)

    @staticmethod
    def _build_registry(config: ForgeConfig) -> ToolRegistry:
        """Load the deployment's own tools, or fall back to the examples.

        A deployment that has not pointed `tools_module` at its own registry
        gets the bundled example tools. Those are a four-string corpus for the
        demo and the test suite - useful for learning the shape, useless for
        anything real. `forge init` scaffolds a module to replace them.
        """
        if not config.tools_module:
            from forge.tools.builtin import build_default_registry

            return build_default_registry()

        target = config.tools_module
        module_name, _, attribute = target.partition(":")
        if not attribute:
            raise UnrecoverableError(
                f"tools_module must be 'package.module:attribute'; got {target!r}"
            )
        try:
            module = import_module(module_name)
        except ImportError:
            # A project-local `tools.py` is the documented layout, but a
            # console-script entry point does not put the working directory on
            # sys.path the way `python -m` does. Honour the convention we told
            # people to use rather than making them export PYTHONPATH.
            cwd = str(Path.cwd())
            if cwd not in sys.path:
                sys.path.insert(0, cwd)
            try:
                module = import_module(module_name)
            except ImportError as exc:
                raise UnrecoverableError(
                    f"cannot import tools module {module_name!r}: {exc}. "
                    f"Looked in {cwd} and on PYTHONPATH. Is the file named "
                    f"{module_name}.py and in this directory?"
                ) from exc

        registry = getattr(module, attribute, None)
        if registry is None:
            raise UnrecoverableError(
                f"{module_name!r} has no attribute {attribute!r}"
            )
        if not isinstance(registry, ToolRegistry):
            raise UnrecoverableError(
                f"{target} is a {type(registry).__name__}, expected a ToolRegistry"
            )
        return registry

    def _build_providers(self) -> list[LLMProvider]:
        """Instantiate the routing chain from config, in declared order."""
        if self._providers is not None:
            return self._providers

        built: list[LLMProvider] = []
        for spec in self.config.providers:
            built.append(_provider_from(spec))
        return built or [MockProvider.answering("no provider configured")]

    def _build_policy(self) -> PolicyBundle:
        if self._policy_bundle is not None:
            return self._policy_bundle
        path = Path(self.config.policy_bundle) if self.config.policy_bundle else PACKAGED_POLICY
        if not path.exists():
            raise UnrecoverableError(f"policy bundle not found: {path}")
        bundle = PolicyBundle.from_yaml(path)
        budget = self.config.budget
        bundle.budget.max_usd = budget.max_usd
        bundle.budget.max_tokens = budget.max_tokens
        bundle.budget.max_steps = budget.max_steps
        bundle.budget.max_tool_calls = budget.max_tool_calls
        bundle.budget.max_wall_clock_s = budget.max_wall_clock_s
        return bundle

    def _build_runtime(self, providers: Sequence[LLMProvider] | None = None) -> AgentRuntime:
        """A fresh runtime per run - per-run state is never shared."""
        bundle = self._build_policy()
        chain = list(providers) if providers is not None else self._build_providers()
        return AgentRuntime(
            store=self._store,
            gateway=LLMGateway(
                providers=chain,
                ledger=CostLedger(
                    usd_ceiling=bundle.budget.max_usd,
                    token_ceiling=bundle.budget.max_tokens,
                ),
            ),
            registry=self._registry,
            policy=PolicyEngine(bundle, available_isolation=self.available_isolation),
            compiler=ContextCompiler(),
            config=RuntimeConfig(
                max_steps=bundle.budget.max_steps,
                checkpoint_every=self.config.checkpoint_every,
                seed=self.config.seed,
            ),
            tracer=Tracer(otel=self.config.telemetry.otel),
            metrics=self.metrics,
            approval=self.approval,
        )

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> Forge:
        if not self._opened:
            await self._store.open()
            self._opened = True
        return self

    async def close(self) -> None:
        if self._opened:
            await self._store.close()
            self._opened = False
        for provider in self._providers or []:
            with suppress(Exception):
                aclose = getattr(provider, "aclose", None)
                if aclose is not None:
                    await aclose()

    async def __aenter__(self) -> Forge:
        return await self.open()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    def _require_open(self) -> None:
        if not self._opened:
            raise UnrecoverableError(
                "harness is not open; use `async with Forge(...)` or `await forge.open()`"
            )

    # -- the API -----------------------------------------------------------

    async def run(
        self,
        goal: str | TaskSpec,
        *,
        tools: Sequence[str] | None = None,
        max_steps: int | None = None,
        run_id: str | None = None,
    ) -> RunResult:
        """Execute a task to completion.

        Concurrency is bounded by `max_concurrent_runs`; callers can fan out
        without having to build their own queue.
        """
        self._require_open()
        task = goal if isinstance(goal, TaskSpec) else TaskSpec(
            goal=goal,
            tools=list(tools if tools is not None else self.config.tools),
            max_steps=max_steps or self.config.budget.max_steps,
        )
        async with self._gate:
            return await self._build_runtime().start(task, run_id=run_id)

    async def resume(self, run_id: str) -> RunResult:
        """Continue an interrupted run from its last checkpoint."""
        self._require_open()
        async with self._gate:
            return await self._build_runtime().resume(run_id)

    async def run_many(self, goals: Sequence[str], **kwargs: Any) -> list[RunResult]:
        """Execute several tasks concurrently, respecting the concurrency gate."""
        self._require_open()
        return list(await asyncio.gather(*(self.run(g, **kwargs) for g in goals)))

    # -- inspection --------------------------------------------------------

    async def events(self, run_id: str, *, after_seq: int = 0) -> list[Event]:
        self._require_open()
        return await self._store.read(run_id, after_seq=after_seq)

    async def runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self._require_open()
        return await self._store.list_runs(limit=limit)

    async def checkpoint(self, run_id: str) -> Any:
        self._require_open()
        return await self._store.latest_checkpoint(run_id)

    @property
    def tools(self) -> list[str]:
        return self._registry.names()

    @property
    def policy(self) -> PolicyBundle:
        return self._build_policy()

    async def health(self) -> dict[str, Any]:
        """Readiness detail for `/healthz` and `forge doctor`."""
        providers = []
        for provider in self._build_providers():
            healthy = True
            with suppress(Exception):
                healthy = await provider.healthy()
            entry: dict[str, Any] = {
                "name": provider.name,
                "model": provider.model,
                "healthy": healthy,
            }
            # A provider that can say *why* it is unusable should; "not
            # reachable" sends someone to check the network when the real
            # answer is that they never pulled the model.
            if not healthy:
                diagnose = getattr(provider, "diagnose", None)
                if diagnose is not None:
                    with suppress(Exception):
                        entry["detail"] = await diagnose()
            providers.append(entry)
        return {
            "ok": self._opened and any(p["healthy"] for p in providers),
            "store_open": self._opened,
            "providers": providers,
            "tools": self.tools,
            "policy_version": self.policy.version,
        }


def _provider_from(spec: ProviderConfig) -> LLMProvider:
    """Build one provider from its configuration entry."""
    pricing = Pricing(input_per_1k=spec.input_per_1k, output_per_1k=spec.output_per_1k)

    match spec.kind:
        case "mock":
            return MockProvider.answering("mock provider: no model configured")
        case "ollama":
            return OllamaProvider(
                model=spec.model,
                host=spec.base_url or "http://127.0.0.1:11434",
                num_ctx=spec.num_ctx,
                timeout_s=spec.timeout_s,
            )
        case "openai":
            return OpenAICompatProvider(
                model=spec.model,
                base_url=spec.base_url or "https://api.openai.com/v1",
                api_key=spec.api_key,
                pricing=pricing,
                timeout_s=spec.timeout_s,
            )
        case _:
            raise UnrecoverableError(
                f"unknown provider kind {spec.kind!r}; expected mock, ollama or openai"
            )
