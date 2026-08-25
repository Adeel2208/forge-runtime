"""Typed tool registry (spec §12).

Every tool declares a Pydantic argument model, a side-effect class, a risk
class and the capability it requires. None of these have defaults that fail
open: a tool with no declared capability is unusable, not universally usable.

Registration is a decorator, but the decorator is thin - it only assembles a
`ToolSpec`. Tools stay ordinary async functions, testable without the runtime.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from forge.core.enums import RiskClass, SideEffect
from forge.errors import DeterministicError, TransientError
from forge.ids import content_hash

__all__ = ["ToolOutcome", "ToolRegistry", "ToolSpec", "tool"]

ToolFn = Callable[..., Awaitable[Any]]


@dataclass
class ToolOutcome:
    """What a tool invocation produced, before the runtime interprets it."""

    ok: bool
    output: Any = None
    error: str | None = None
    latency_ms: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    """The complete contract for one tool."""

    name: str
    description: str
    args_model: type[BaseModel]
    fn: ToolFn
    side_effect: SideEffect
    capability: str
    risk: RiskClass = RiskClass.LOW
    timeout_s: float = 30.0
    version: str = "1.0.0"
    supports_dry_run: bool = False
    compensate: ToolFn | None = None
    """Undo for a reversible write. Required if side_effect is REVERSIBLE_WRITE."""

    max_concurrency: int = 4

    def __post_init__(self) -> None:
        if self.side_effect is SideEffect.REVERSIBLE_WRITE and self.compensate is None:
            raise ValueError(
                f"tool {self.name!r} is REVERSIBLE_WRITE but declares no compensate()"
            )
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    def json_schema(self) -> dict[str, Any]:
        """The schema handed to the model. Includes the effect class on purpose:
        a model that knows an action is irreversible proposes it less casually.
        """
        return {
            "name": self.name,
            "description": self.description,
            "side_effect": self.side_effect.value,
            "risk": self.risk.value,
            "parameters": self.args_model.model_json_schema(),
        }

    def validate_args(self, arguments: dict[str, Any]) -> BaseModel:
        try:
            return self.args_model.model_validate(arguments)
        except ValidationError as exc:
            raise DeterministicError(
                f"invalid arguments for tool {self.name!r}",
                tool=self.name,
                detail=exc.errors(include_url=False),
            ) from exc

    def fingerprint(self, arguments: dict[str, Any]) -> str:
        """Identity of an *intended* action, for loop detection."""
        return content_hash(self.name, self.version, arguments)[:16]

    async def invoke(self, arguments: dict[str, Any], *, dry_run: bool = False) -> ToolOutcome:
        """Run the tool under its own timeout and concurrency limit."""
        validated = self.validate_args(arguments)
        payload = validated.model_dump()

        if dry_run:
            if not self.supports_dry_run:
                raise DeterministicError(
                    f"tool {self.name!r} does not support dry-run", tool=self.name
                )
            payload["_dry_run"] = True

        started = time.monotonic()
        try:
            async with self._semaphore:
                result = await asyncio.wait_for(self.fn(**payload), timeout=self.timeout_s)
        except TimeoutError as exc:
            raise TransientError(
                f"tool {self.name!r} timed out after {self.timeout_s}s", tool=self.name
            ) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        if isinstance(result, ToolOutcome):
            result.latency_ms = latency_ms
            return result
        return ToolOutcome(ok=True, output=result, latency_ms=latency_ms)


class ToolRegistry:
    """A namespace of tools. Lookup of an unregistered tool is an error."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise DeterministicError(
                f"unknown tool {name!r}", tool=name, known=sorted(self._tools)
            ) from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, allow: list[str] | None = None) -> list[dict[str, Any]]:
        """Schemas for the allow-listed tools only.

        An empty allow-list yields nothing: the task must opt in to each tool,
        so a prompt-injected request for a tool the task never granted has
        nothing to bind to.
        """
        chosen = self.names() if allow is None else [n for n in allow if n in self._tools]
        return [self._tools[n].json_schema() for n in chosen]

    def tool(
        self,
        *,
        name: str | None = None,
        description: str = "",
        args: type[BaseModel],
        side_effect: SideEffect = SideEffect.READ,
        capability: str,
        risk: RiskClass = RiskClass.LOW,
        timeout_s: float = 30.0,
        version: str = "1.0.0",
        supports_dry_run: bool = False,
        compensate: ToolFn | None = None,
    ) -> Callable[[ToolFn], ToolFn]:
        """Decorator form of `register`."""

        def decorator(fn: ToolFn) -> ToolFn:
            self.register(
                ToolSpec(
                    name=name or fn.__name__,
                    description=description or inspect.getdoc(fn) or "",
                    args_model=args,
                    fn=fn,
                    side_effect=side_effect,
                    capability=capability,
                    risk=risk,
                    timeout_s=timeout_s,
                    version=version,
                    supports_dry_run=supports_dry_run,
                    compensate=compensate,
                )
            )
            return fn

        return decorator


def tool(registry: ToolRegistry, **kwargs: Any) -> Callable[[ToolFn], ToolFn]:
    """Module-level alias: ``@tool(registry, args=..., capability=...)``."""
    return registry.tool(**kwargs)
