"""The failure-injection benchmark (spec §17, §18).

Runs a fixed task set against each fault class and reports what the runtime
did about it. The control arm (`FaultClass.NONE`) is mandatory: "recovered
from a crash" only means something next to "and here is the run that had no
crash".

Every trial is seeded and runs on `MockProvider`, so the whole suite is free,
offline, and reproducible by anyone who clones the repo.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from forge.core.contracts import TaskSpec
from forge.core.enums import RunStatus
from forge.evaluation.faults import FaultClass, FaultInjector
from forge.llm.mock import MockProvider, ScriptedTurn
from forge.runtime.loop import AgentRuntime, RuntimeConfig, SimulatedCrash
from forge.security.policy import PolicyBundle, PolicyEngine
from forge.state.sqlite_store import SQLiteEventStore
from forge.tools.builtin import build_default_registry, set_flakiness

__all__ = ["DEFAULT_TASKS", "BenchmarkReport", "BenchmarkRunner", "BenchmarkTask", "TrialResult"]


@dataclass
class BenchmarkTask:
    """A task plus the model script that drives it. Fixed at M5, never tuned."""

    name: str
    goal: str
    tools: list[str]
    script: list[dict[str, Any]]
    expect_substring: str = ""


DEFAULT_TASKS: list[BenchmarkTask] = [
    BenchmarkTask(
        name="lookup_and_answer",
        goal="What did FORGE measure about context compilation?",
        tools=["search_corpus", "read_document"],
        script=[
            {"proposal": {"kind": "TOOL_CALL", "tool": "search_corpus",
                          "arguments": {"query": "context"}}},
            {"proposal": {"kind": "TOOL_CALL", "tool": "read_document",
                          "arguments": {"key": "context"}}},
            {"proposal": {"kind": "ANSWER",
                          "answer": "Context compilation reduced token usage by 38%."}},
        ],
        expect_substring="38%",
    ),
    BenchmarkTask(
        name="calculate_then_write",
        goal="Compute 38 * 2 and save the result as a note.",
        tools=["calculate", "save_note"],
        script=[
            {"proposal": {"kind": "TOOL_CALL", "tool": "calculate",
                          "arguments": {"expression": "38 * 2"}}},
            {"proposal": {"kind": "TOOL_CALL", "tool": "save_note",
                          "arguments": {"name": "result", "content": "76"}}},
            {"proposal": {"kind": "ANSWER", "answer": "76, saved as note 'result'."}},
        ],
        expect_substring="76",
    ),
    BenchmarkTask(
        name="denied_publish",
        goal="Publish the findings externally.",
        tools=["search_corpus", "publish"],
        script=[
            {"proposal": {"kind": "TOOL_CALL", "tool": "publish",
                          "arguments": {"destination": "blog", "body": "findings"}}},
            {"proposal": {"kind": "ANSWER",
                          "answer": "Publishing was refused by policy; findings not sent."}},
        ],
        expect_substring="refused",
    ),
]


@dataclass
class TrialResult:
    task: str
    fault: str
    trial: int
    status: str
    recovered: bool
    """The run reached COMPLETED despite the fault."""

    contained: bool = False
    """The runtime handled the fault *correctly*, which is not always the same
    as completing. For an unbreakable action loop the correct outcome is a
    deliberate halt, so scoring it only on completion would penalise the
    runtime for doing the right thing. Reported alongside recovery, never
    instead of it."""

    resumed: bool = False
    steps: int = 0
    tokens: int = 0
    usd: float = 0.0
    duration_ms: int = 0
    duplicate_effects: int = 0
    denials: int = 0
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == RunStatus.COMPLETED.value


@dataclass
class BenchmarkReport:
    results: list[TrialResult] = field(default_factory=list)
    seed: int = 1729
    started_at: str = ""
    runtime_version: str = ""

    # -- aggregation -------------------------------------------------------

    def by_fault(self) -> dict[str, dict[str, Any]]:
        buckets: dict[str, list[TrialResult]] = {}
        for r in self.results:
            buckets.setdefault(r.fault, []).append(r)

        control = buckets.get(FaultClass.NONE.value, [])
        base_latency = _mean([r.duration_ms for r in control]) if control else 0.0
        base_tokens = _mean([r.tokens for r in control]) if control else 0.0

        summary: dict[str, dict[str, Any]] = {}
        for fault, rows in buckets.items():
            n = len(rows)
            succeeded = sum(1 for r in rows if r.succeeded)
            latency = _mean([r.duration_ms for r in rows])
            tokens = _mean([r.tokens for r in rows])
            summary[fault] = {
                "trials": n,
                "task_success_rate": round(succeeded / n, 4) if n else 0.0,
                "recovery_rate": round(sum(1 for r in rows if r.recovered) / n, 4) if n else 0.0,
                "containment_rate": round(sum(1 for r in rows if r.contained) / n, 4) if n else 0.0,
                "duplicate_effects": sum(r.duplicate_effects for r in rows),
                "policy_denials": sum(r.denials for r in rows),
                "mean_latency_ms": round(latency, 1),
                "added_latency_ms": round(latency - base_latency, 1),
                "mean_tokens": round(tokens, 1),
                "added_tokens": round(tokens - base_tokens, 1),
                "usd": round(sum(r.usd for r in rows), 6),
            }
        return summary

    def to_json(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "started_at": self.started_at,
            "runtime_version": self.runtime_version,
            "summary": self.by_fault(),
            "trials": [asdict(r) for r in self.results],
        }

    def to_markdown(self) -> str:
        summary = self.by_fault()
        lines = [
            "# FORGE failure-injection report",
            "",
            f"- runtime version: `{self.runtime_version}`",
            f"- seed: `{self.seed}` (every trial is reproducible)",
            f"- generated: {self.started_at}",
            f"- trials: {len(self.results)}",
            "",
            "| Injected failure | Trials | Task success | Recovered | Contained "
            "| Dup effects | +Latency (ms) | +Tokens |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        # Control arm first, then the rest alphabetically - the deltas in every
        # other row are measured against it, so it has to be read first.
        order = [
            FaultClass.NONE.value,
            *sorted(k for k in summary if k != FaultClass.NONE.value),
        ]
        for fault in order:
            row = summary.get(fault)
            if row is None:
                continue
            lines.append(
                f"| `{fault}` | {row['trials']} | {row['task_success_rate']:.0%} "
                f"| {row['recovery_rate']:.0%} | {row['containment_rate']:.0%} "
                f"| {row['duplicate_effects']} "
                f"| {row['added_latency_ms']:+.1f} | {row['added_tokens']:+.1f} |"
            )
        lines += [
            "",
            "**Reading this table.**",
            "",
            "- **Recovered** - the run reached `COMPLETED` despite the fault.",
            "- **Contained** - the runtime responded *correctly*, which is not "
            "always the same thing. For `repeated_action_loop` the correct "
            "response is a deliberate halt, so a bounded run counts as "
            "contained but not recovered. Both columns are reported because "
            "either alone would flatter the system.",
            "- **Dup effects** - external effects observed more than once. This "
            "is the column that must read `0` for crash-resume to be safe; it "
            "is the whole point of the idempotency machinery.",
            "- Latency and token deltas are measured against the `none` control "
            "arm, which is why that row is always `+0.0`.",
        ]
        return "\n".join(lines) + "\n"

    def write(self, directory: str | Path) -> tuple[Path, Path]:
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        json_path = out / "failure_injection.json"
        md_path = out / "failure_injection.md"
        json_path.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")
        md_path.write_text(self.to_markdown(), encoding="utf-8")
        return json_path, md_path


class BenchmarkRunner:
    """Executes the task set under each fault class."""

    def __init__(
        self,
        *,
        tasks: list[BenchmarkTask] | None = None,
        faults: list[FaultClass] | None = None,
        trials: int = 1,
        seed: int = 1729,
        db_dir: str | Path = ".forge/bench",
    ) -> None:
        self.tasks = tasks or DEFAULT_TASKS
        self.faults = faults or [
            FaultClass.NONE,
            FaultClass.WORKER_CRASH,
            FaultClass.LLM_TIMEOUT,
            FaultClass.MALFORMED_OUTPUT,
            FaultClass.TOOL_TIMEOUT,
            FaultClass.POLICY_DENIAL,
            FaultClass.REPEATED_ACTION_LOOP,
        ]
        self.trials = trials
        self.seed = seed
        self.db_dir = Path(db_dir)

    async def run(self) -> BenchmarkReport:
        from datetime import UTC, datetime

        from forge import __version__

        report = BenchmarkReport(
            seed=self.seed,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
            runtime_version=__version__,
        )
        for task in self.tasks:
            for fault in self.faults:
                for trial in range(self.trials):
                    report.results.append(await self._one(task, fault, trial))
        return report

    async def _one(self, task: BenchmarkTask, fault: FaultClass, trial: int) -> TrialResult:
        set_flakiness(0)
        seed = self.seed + trial
        db = self.db_dir / f"{task.name}_{fault.value}_{trial}.db"
        if db.exists():
            db.unlink()

        store = SQLiteEventStore(db)
        await store.open()
        started = time.monotonic()
        recovered = resumed = False
        error: str | None = None

        try:
            runtime = self._build(store, task, fault, seed)
            spec = TaskSpec(goal=task.goal, tools=task.tools, max_steps=12)

            try:
                result = await runtime.start(spec)
            except SimulatedCrash as crash:
                # The worker "died". Build a brand-new runtime - as a restarted
                # process would - and resume from the checkpoint.
                error = str(crash)
                resumed = True
                run_id = (await self._latest_run(store)) or ""
                fresh = self._build(store, task, FaultClass.NONE, seed)
                result = await fresh.resume(run_id)
                recovered = result.status is RunStatus.COMPLETED

            if not resumed:
                recovered = result.status is RunStatus.COMPLETED

            return TrialResult(
                task=task.name,
                fault=fault.value,
                trial=trial,
                status=result.status.value,
                recovered=recovered,
                contained=await self._is_contained(store, result, fault, recovered),
                resumed=resumed,
                steps=result.steps,
                tokens=result.usage.total_tokens,
                usd=result.usage.usd,
                duration_ms=int((time.monotonic() - started) * 1000),
                duplicate_effects=result.duplicate_effects,
                denials=len(result.denials),
                error=error or result.error,
            )
        finally:
            await store.close()

    def _build(
        self, store: SQLiteEventStore, task: BenchmarkTask, fault: FaultClass, seed: int
    ) -> AgentRuntime:
        from forge.llm.gateway import CostLedger, LLMGateway

        provider = MockProvider(self._script_for(task, fault))
        registry = build_default_registry()
        bundle = PolicyBundle.baseline(granted=["KNOWLEDGE_READ", "CALC", "WORKSPACE_WRITE"])
        injector = (
            FaultInjector.none()
            if FaultInjector.shapes_script(fault) or fault is FaultClass.NONE
            else FaultInjector.single(fault, at_step=1, seed=seed)
        )
        return AgentRuntime(
            store=store,
            gateway=LLMGateway(providers=[provider], ledger=CostLedger(usd_ceiling=0.0)),
            registry=registry,
            policy=PolicyEngine(bundle),
            config=RuntimeConfig(seed=seed, max_steps=12),
            faults=injector,
        )

    @staticmethod
    def _script_for(task: BenchmarkTask, fault: FaultClass) -> list[ScriptedTurn]:
        """Some faults are induced by shaping the model script, not by raising."""
        turns = [ScriptedTurn(**t) for t in task.script]

        if fault is FaultClass.MALFORMED_OUTPUT:
            first = turns[0]
            return [first.model_copy(update={"malformed": True}), *turns]

        if fault is FaultClass.REPEATED_ACTION_LOOP:
            first = turns[0]
            return [first.model_copy(update={"repeat": 5}), *turns[1:]]

        return turns

    @staticmethod
    async def _is_contained(
        store: SQLiteEventStore, result: Any, fault: FaultClass, recovered: bool
    ) -> bool:
        """Did the runtime respond to this fault correctly?

        Completing is the usual correct response. The exception is an
        unbreakable action loop: there the correct response is to stop, so a
        deliberate halt with a LOOP_DETECTED event counts as containment even
        though the task did not finish.
        """
        if recovered:
            return True
        if fault is FaultClass.REPEATED_ACTION_LOOP:
            from forge.core.enums import EventType

            events = await store.read(result.run_id)
            return any(e.type is EventType.LOOP_DETECTED for e in events)
        return False

    @staticmethod
    async def _latest_run(store: SQLiteEventStore) -> str | None:
        runs = await store.list_runs(limit=1)
        return str(runs[0]["run_id"]) if runs else None


def _mean(values: list[float] | list[int]) -> float:
    return (sum(values) / len(values)) if values else 0.0
