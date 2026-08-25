"""A worker process that dies hard, for the out-of-process recovery test.

Invoked as ``python -m tests.recovery._crash_worker <db> <run_id> <crash_step>``.

The crash uses ``os._exit()``, which terminates immediately: no exception
propagation, no ``finally`` blocks, no ``atexit`` hooks, no buffer flush. That
is the point - a test whose "crash" can be caught and cleaned up proves
nothing about surviving a real SIGKILL.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from forge.core.contracts import TaskSpec
from forge.llm.gateway import CostLedger, LLMGateway
from forge.llm.mock import MockProvider
from forge.runtime.loop import AgentRuntime, RuntimeConfig
from forge.security.policy import PolicyBundle, PolicyEngine
from forge.state.sqlite_store import SQLiteEventStore
from forge.tools.builtin import build_default_registry

SCRIPT: list[dict[str, Any]] = [
    {"proposal": {"kind": "TOOL_CALL", "tool": "calculate",
                  "arguments": {"expression": "10 + 5"}}},
    {"proposal": {"kind": "TOOL_CALL", "tool": "save_note",
                  "arguments": {"name": "step2", "content": "fifteen"}}},
    {"proposal": {"kind": "TOOL_CALL", "tool": "save_note",
                  "arguments": {"name": "step3", "content": "final"}}},
    {"proposal": {"kind": "ANSWER", "answer": "15, saved."}},
]

EXIT_CRASHED = 137  # conventionally "killed by SIGKILL"


class HardCrash:
    """A fault injector that kills the process outright."""

    def __init__(self, at_step: int) -> None:
        self.at_step = at_step

    async def check(self, hook: str, **ctx: Any) -> None:
        if hook == "before_dispatch" and int(ctx.get("step") or 0) == self.at_step:
            sys.stderr.write(f"worker: hard exit at step {self.at_step}\n")
            sys.stderr.flush()
            os._exit(EXIT_CRASHED)


def build(store: SQLiteEventStore, faults: Any = None) -> AgentRuntime:
    return AgentRuntime(
        store=store,
        gateway=LLMGateway(providers=[MockProvider(SCRIPT)], ledger=CostLedger()),
        registry=build_default_registry(),
        policy=PolicyEngine(
            PolicyBundle.baseline(granted=["KNOWLEDGE_READ", "CALC", "WORKSPACE_WRITE"])
        ),
        config=RuntimeConfig(max_steps=8),
        faults=faults,
    )


async def _main(db: str, run_id: str, crash_step: int) -> int:
    store = SQLiteEventStore(db)
    await store.open()
    runtime = build(store, HardCrash(crash_step))
    await runtime.start(
        TaskSpec(goal="crash test", tools=["calculate", "save_note"], max_steps=8),
        run_id=run_id,
    )
    await store.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1], sys.argv[2], int(sys.argv[3]))))
