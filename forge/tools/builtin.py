"""A small tool set spanning all three side-effect classes.

Chosen so the demo and the benchmarks exercise every authorization path:
a READ tool that always works, a READ tool that fails transiently, a
REVERSIBLE_WRITE with a real compensator, and an IRREVERSIBLE_WRITE that the
default policy refuses outright.

The corpus is deliberately tiny and in-process: benchmarks must be free,
offline and deterministic.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forge.core.enums import RiskClass, SideEffect
from forge.errors import TransientError
from forge.tools.registry import ToolOutcome, ToolRegistry

__all__ = ["CORPUS", "WORKSPACE", "build_default_registry"]


CORPUS: dict[str, str] = {
    "checkpointing": (
        "Durable checkpointing lets a long-horizon run resume after worker "
        "failure. Measured recovery rates depend on checkpoint frequency: "
        "per-step checkpoints recover 100% of interrupted runs in FORGE's "
        "harness, at a storage cost of roughly 2 KB per step."
    ),
    "idempotency": (
        "Idempotency keys are derived from run id, tool name and canonical "
        "arguments. A retried write reuses its key, so the event store's "
        "unique index suppresses the duplicate rather than the application "
        "remembering to."
    ),
    "context": (
        "Context compilation reduced token usage by 38% versus naive history "
        "concatenation on the same task set, with no measured loss in task "
        "success rate."
    ),
    "evaluation": (
        "Trajectory-level metrics predicted deployment failures more reliably "
        "than final-answer scoring, which missed 41% of runs that reached a "
        "correct answer through an unsafe path."
    ),
}

WORKSPACE: dict[str, str] = {}
"""In-memory scratch space for the reversible-write tool."""


class SearchArgs(BaseModel):
    query: str = Field(description="A topic keyword to look up in the corpus.")


class ReadArgs(BaseModel):
    key: str = Field(description="Exact corpus key returned by search_corpus.")


class CalcArgs(BaseModel):
    expression: str = Field(description="Arithmetic over integers, e.g. '38 * 2 + 4'.")


class NoteArgs(BaseModel):
    name: str
    content: str


class PublishArgs(BaseModel):
    destination: str
    body: str


def build_default_registry() -> ToolRegistry:
    """Fresh registry per run - tools carry semaphores, so no global sharing."""
    registry = ToolRegistry()

    @registry.tool(
        description="Find corpus keys matching a topic. Returns a list of keys.",
        args=SearchArgs,
        side_effect=SideEffect.READ,
        capability="KNOWLEDGE_READ",
    )
    async def search_corpus(query: str) -> list[str]:
        needle = query.lower().strip()
        hits = [k for k in CORPUS if needle in k or needle in CORPUS[k].lower()]
        return hits or sorted(CORPUS)[:3]

    @registry.tool(
        description="Read the full text stored under a corpus key.",
        args=ReadArgs,
        side_effect=SideEffect.READ,
        capability="KNOWLEDGE_READ",
    )
    async def read_document(key: str) -> ToolOutcome:
        if key not in CORPUS:
            return ToolOutcome(ok=False, error=f"no document {key!r}")
        return ToolOutcome(ok=True, output=CORPUS[key], evidence={"key": key, "chars": len(CORPUS[key])})

    @registry.tool(
        description="Evaluate integer arithmetic. Supports + - * / // % ** and parentheses.",
        args=CalcArgs,
        side_effect=SideEffect.READ,
        capability="CALC",
    )
    async def calculate(expression: str) -> ToolOutcome:
        try:
            value = _safe_eval(expression)
        except (ValueError, SyntaxError, ZeroDivisionError, TypeError) as exc:
            return ToolOutcome(ok=False, error=f"cannot evaluate {expression!r}: {exc}")
        return ToolOutcome(ok=True, output=value, evidence={"expression": expression})

    async def _erase_note(name: str, content: str) -> None:
        del content
        WORKSPACE.pop(name, None)

    @registry.tool(
        description="Save a note into the run workspace. Reversible.",
        args=NoteArgs,
        side_effect=SideEffect.REVERSIBLE_WRITE,
        capability="WORKSPACE_WRITE",
        risk=RiskClass.MEDIUM,
        compensate=_erase_note,
    )
    async def save_note(name: str, content: str) -> ToolOutcome:
        WORKSPACE[name] = content
        return ToolOutcome(
            ok=True,
            output=f"saved {name} ({len(content)} chars)",
            evidence={"applied": True, "name": name},
        )

    @registry.tool(
        description="Publish externally. Irreversible - requires human approval.",
        args=PublishArgs,
        side_effect=SideEffect.IRREVERSIBLE_WRITE,
        capability="EXTERNAL_PUBLISH",
        risk=RiskClass.HIGH,
        supports_dry_run=True,
    )
    async def publish(destination: str, body: str, _dry_run: bool = False) -> ToolOutcome:
        if _dry_run:
            return ToolOutcome(ok=True, output=f"[dry-run] would publish {len(body)}b to {destination}")
        return ToolOutcome(
            ok=True,
            output=f"published {len(body)} bytes to {destination}",
            evidence={"applied": True, "destination": destination},
        )

    @registry.tool(
        description="A dependency that fails transiently. Used to exercise retries.",
        args=SearchArgs,
        side_effect=SideEffect.READ,
        capability="KNOWLEDGE_READ",
    )
    async def flaky_lookup(query: str) -> ToolOutcome:
        _FLAKE_STATE["calls"] += 1
        if _FLAKE_STATE["calls"] <= _FLAKE_STATE["fail_first"]:
            raise TransientError("upstream unavailable", query=query)
        return ToolOutcome(ok=True, output=f"resolved {query}", evidence={"attempts": _FLAKE_STATE["calls"]})

    return registry


_FLAKE_STATE: dict[str, int] = {"calls": 0, "fail_first": 0}


def set_flakiness(fail_first: int) -> None:
    """Configure `flaky_lookup` to fail its first N calls, then succeed."""
    _FLAKE_STATE["calls"] = 0
    _FLAKE_STATE["fail_first"] = fail_first


def _safe_eval(expression: str) -> Any:
    """Arithmetic only. Evaluates an AST allow-list, never `eval` on input."""
    import ast
    import operator

    ops: dict[type[ast.operator], Any] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }

    def walk(node: ast.AST) -> Any:
        match node:
            case ast.Expression():
                return walk(node.body)
            case ast.Constant() if isinstance(node.value, int | float):
                return node.value
            case ast.BinOp() if type(node.op) in ops:
                left, right = walk(node.left), walk(node.right)
                if type(node.op) is ast.Pow and (abs(right) > 32 or abs(left) > 10_000):
                    raise ValueError("exponent out of bounds")
                return ops[type(node.op)](left, right)
            case ast.UnaryOp() if isinstance(node.op, ast.USub):
                return -walk(node.operand)
            case ast.UnaryOp() if isinstance(node.op, ast.UAdd):
                return +walk(node.operand)
            case _:
                raise ValueError(f"unsupported expression element: {type(node).__name__}")

    return walk(ast.parse(expression, mode="eval"))
