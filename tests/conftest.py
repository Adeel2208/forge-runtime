"""Shared test fixtures.

Async tests run via `asyncio.run` inside ordinary sync test functions rather
than through `pytest-asyncio`. That is a deliberate dependency choice: the
suite has to run on a clean checkout with nothing but pytest installed, and
each test gets a fresh event loop for free.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest

from forge.context.compiler import ContextCompiler
from forge.evaluation.faults import FaultInjector
from forge.llm.gateway import CostLedger, LLMGateway
from forge.llm.mock import MockProvider
from forge.runtime.loop import AgentRuntime, RuntimeConfig
from forge.security.policy import PolicyBundle, PolicyEngine
from forge.state.sqlite_store import SQLiteEventStore
from forge.tools.builtin import build_default_registry, set_flakiness

T = TypeVar("T")

DEFAULT_GRANTS = ["KNOWLEDGE_READ", "CALC", "WORKSPACE_WRITE"]


def run(coro: Coroutine[Any, Any, T]) -> T:
    """Drive a coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_global_tool_state() -> None:
    """Builtin tools keep a little module state; reset it between tests."""
    from forge.tools.builtin import WORKSPACE

    WORKSPACE.clear()
    set_flakiness(0)


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "events.db"


@pytest.fixture
def make_store(store_path: Path) -> Callable[[], SQLiteEventStore]:
    def factory() -> SQLiteEventStore:
        return SQLiteEventStore(store_path)

    return factory


@pytest.fixture
def make_runtime(store_path: Path) -> Callable[..., AgentRuntime]:
    """Build a runtime over a shared on-disk store.

    Each call produces a *new* runtime object against the same database, which
    is how the recovery tests model "a fresh worker picked up the run".
    """

    def factory(
        script: list[dict[str, Any]] | MockProvider,
        *,
        store: SQLiteEventStore | None = None,
        grants: list[str] | None = None,
        faults: FaultInjector | None = None,
        config: RuntimeConfig | None = None,
        auto_approve: bool = False,
        usd_ceiling: float = 0.0,
    ) -> AgentRuntime:
        provider = script if isinstance(script, MockProvider) else MockProvider(script)
        bundle = PolicyBundle.baseline(
            granted=DEFAULT_GRANTS if grants is None else grants
        )
        cfg = config or RuntimeConfig()
        if auto_approve:
            cfg.auto_approve = True
        return AgentRuntime(
            store=store or SQLiteEventStore(store_path),
            gateway=LLMGateway(
                providers=[provider], ledger=CostLedger(usd_ceiling=usd_ceiling)
            ),
            registry=build_default_registry(),
            policy=PolicyEngine(bundle),
            compiler=ContextCompiler(token_budget=2000),
            config=cfg,
            faults=faults,
        )

    return factory


def answer_script(answer: str = "done") -> list[dict[str, Any]]:
    return [{"proposal": {"kind": "ANSWER", "answer": answer}}]


def lookup_script() -> list[dict[str, Any]]:
    """search -> read -> answer. The canonical happy path."""
    return [
        {"proposal": {"kind": "TOOL_CALL", "tool": "search_corpus",
                      "arguments": {"query": "idempotency"}}},
        {"proposal": {"kind": "TOOL_CALL", "tool": "read_document",
                      "arguments": {"key": "idempotency"}}},
        {"proposal": {"kind": "ANSWER", "answer": "Keys are derived from canonical arguments."}},
    ]


def write_script() -> list[dict[str, Any]]:
    """calculate -> save_note -> answer. Exercises a reversible write."""
    return [
        {"proposal": {"kind": "TOOL_CALL", "tool": "calculate",
                      "arguments": {"expression": "19 * 4"}}},
        {"proposal": {"kind": "TOOL_CALL", "tool": "save_note",
                      "arguments": {"name": "answer", "content": "76"}}},
        {"proposal": {"kind": "ANSWER", "answer": "76"}},
    ]
