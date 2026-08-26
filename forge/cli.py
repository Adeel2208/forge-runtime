"""The `forge` command line.

Every verb here exists to make a spec §27 acceptance criterion checkable by
hand: `run` then `resume` proves crash recovery, `trace` proves the audit
trail, `replay` proves determinism, `bench` produces the report.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import typer

from forge.core.contracts import TaskSpec
from forge.eval.cases import CaseSet, CaseSetError
from forge.evaluation.benchmark import BenchmarkRunner
from forge.evaluation.faults import FaultClass, FaultInjector
from forge.evaluation.replay import replay_run
from forge.llm.gateway import CostLedger, LLMGateway
from forge.llm.mock import MockProvider
from forge.llm.ollama import OllamaProvider
from forge.runtime.loop import AgentRuntime, RuntimeConfig, SimulatedCrash
from forge.security.policy import PolicyBundle, PolicyEngine
from forge.state.sqlite_store import SQLiteEventStore
from forge.tools.builtin import build_default_registry

app = typer.Typer(
    add_completion=False,
    help="FORGE - a durable, policy-aware execution runtime for long-horizon AI agents.",
)

# `forge eval ...` - the evaluation harness. Kept in its own module because the
# harness must not depend on the runtime CLI, only on the Target interface.
from forge.eval.cli import app as eval_app  # noqa: E402

app.add_typer(eval_app, name="eval")

DEFAULT_DB = ".forge/forge.db"
DEFAULT_POLICY = Path(__file__).parent / "security" / "policies" / "default.yaml"
DEFAULT_TOOLS = ["search_corpus", "read_document", "calculate", "save_note"]


def _echo(text: str = "") -> None:
    typer.echo(text)


def _runtime(
    store: SQLiteEventStore,
    *,
    provider_name: str,
    model: str,
    policy_path: Path,
    fault: FaultClass = FaultClass.NONE,
    crash_at: int | None = None,
    seed: int = 1729,
    approve: bool = False,
) -> AgentRuntime:
    if provider_name == "ollama":
        provider: Any = OllamaProvider(model=model)
    else:
        provider = MockProvider(_demo_script())

    injector = FaultInjector.none()
    if crash_at is not None:
        injector = FaultInjector.single(FaultClass.WORKER_CRASH, at_step=crash_at, seed=seed)
    elif fault is not FaultClass.NONE:
        injector = FaultInjector.single(fault, at_step=1, seed=seed)

    bundle = (
        PolicyBundle.from_yaml(policy_path)
        if policy_path.exists()
        else PolicyBundle.baseline(granted=["KNOWLEDGE_READ", "CALC", "WORKSPACE_WRITE"])
    )
    return AgentRuntime(
        store=store,
        gateway=LLMGateway(providers=[provider], ledger=CostLedger(usd_ceiling=bundle.budget.max_usd)),
        registry=build_default_registry(),
        policy=PolicyEngine(bundle),
        config=RuntimeConfig(seed=seed, auto_approve=approve),
        faults=injector,
    )


def _demo_script() -> list[dict[str, Any]]:
    return [
        {"proposal": {"kind": "TOOL_CALL", "tool": "search_corpus",
                      "arguments": {"query": "checkpointing"},
                      "rationale_summary": "Find the relevant document."}},
        {"proposal": {"kind": "TOOL_CALL", "tool": "read_document",
                      "arguments": {"key": "checkpointing"},
                      "rationale_summary": "Read the full text."}},
        {"proposal": {"kind": "TOOL_CALL", "tool": "save_note",
                      "arguments": {"name": "finding", "content": "per-step checkpoints recover 100%"},
                      "rationale_summary": "Record the finding."}},
        {"proposal": {"kind": "ANSWER",
                      "answer": "Per-step checkpointing recovered 100% of interrupted runs, "
                                "at about 2 KB of storage per step.",
                      "rationale_summary": "Evidence gathered; answering."}},
    ]


# ---------------------------------------------------------------- commands


@app.command()
def run(
    goal: Annotated[str, typer.Argument(help="What the agent should accomplish.")] = (
        "What did FORGE measure about durable checkpointing?"
    ),
    db: Annotated[str, typer.Option(help="Event-store path.")] = DEFAULT_DB,
    provider: Annotated[str, typer.Option(help="mock | ollama")] = "mock",
    model: Annotated[str, typer.Option(help="Model id when provider=ollama.")] = "qwen3:8b",
    policy: Annotated[Path, typer.Option(help="Policy bundle YAML.")] = DEFAULT_POLICY,
    crash_at: Annotated[int, typer.Option(help="Inject a worker crash at this step.")] = 0,
    fault: Annotated[str, typer.Option(help="Inject a fault class by name.")] = "none",
    approve: Annotated[bool, typer.Option(help="Auto-approve irreversible actions.")] = False,
    tools: Annotated[str, typer.Option(help="Comma-separated tool allow-list.")] = ",".join(
        DEFAULT_TOOLS
    ),
) -> None:
    """Start a new run."""

    async def main() -> int:
        store = SQLiteEventStore(db)
        await store.open()
        try:
            runtime = _runtime(
                store,
                provider_name=provider,
                model=model,
                policy_path=policy,
                fault=FaultClass(fault),
                crash_at=crash_at or None,
                approve=approve,
            )
            spec = TaskSpec(goal=goal, tools=[t for t in tools.split(",") if t])
            try:
                result = await runtime.start(spec)
            except SimulatedCrash as crash:
                runs = await store.list_runs(limit=1)
                run_id = runs[0]["run_id"] if runs else "?"
                _echo(f"\n  worker crashed: {crash}")
                _echo(f"  run_id: {run_id}")
                _echo(f"\n  resume it with:  forge resume {run_id} --db {db}")
                return 2

            _echo(f"\n  status : {result.status.value}")
            _echo(f"  run_id : {result.run_id}")
            _echo(f"  steps  : {result.steps}")
            _echo(f"  tokens : {result.usage.total_tokens}")
            _echo(f"  cost   : ${result.usage.usd:.4f}")
            if result.denials:
                _echo(f"  denied : {len(result.denials)} action(s) refused by policy")
                for d in result.denials:
                    _echo(f"           step {d['step']}: {d['reason']}")
            if result.answer:
                _echo(f"\n  {result.answer}\n")
            if result.error:
                _echo(f"  error  : {result.error}")
            return 0 if result.ok else 1
        finally:
            await store.close()

    raise typer.Exit(asyncio.run(main()))


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="Run to resume.")],
    db: Annotated[str, typer.Option(help="Event-store path.")] = DEFAULT_DB,
    policy: Annotated[Path, typer.Option()] = DEFAULT_POLICY,
    provider: Annotated[str, typer.Option(help="mock | ollama")] = "mock",
    model: Annotated[str, typer.Option()] = "qwen3:8b",
) -> None:
    """Resume an interrupted run from its latest checkpoint."""

    async def main() -> int:
        store = SQLiteEventStore(db)
        await store.open()
        try:
            runtime = _runtime(
                store, provider_name=provider, model=model, policy_path=policy
            )
            result = await runtime.resume(run_id)
            _echo(f"\n  status            : {result.status.value}")
            _echo(f"  resumed           : {result.resumed}")
            _echo(f"  steps             : {result.steps}")
            _echo(f"  duplicate effects : {result.duplicate_effects}")
            if result.answer:
                _echo(f"\n  {result.answer}\n")
            return 0 if result.ok else 1
        finally:
            await store.close()

    raise typer.Exit(asyncio.run(main()))


@app.command()
def trace(
    run_id: Annotated[str, typer.Argument(help="Run to inspect.")],
    db: Annotated[str, typer.Option()] = DEFAULT_DB,
    full: Annotated[bool, typer.Option(help="Include phase-transition events.")] = False,
) -> None:
    """Print a run's audit trail from the event log."""

    async def main() -> int:
        store = SQLiteEventStore(db)
        await store.open()
        try:
            events = await store.read(run_id)
            if not events:
                _echo(f"no events for run {run_id!r}")
                return 1
            for ev in events:
                if not full and ev.type.value == "PHASE_ENTERED":
                    continue
                _echo("  " + ev.summary())
            ckpt = await store.latest_checkpoint(run_id)
            if ckpt:
                _echo(f"\n  latest checkpoint: step {ckpt.step_index} @ seq {ckpt.last_seq}")
            return 0
        finally:
            await store.close()

    raise typer.Exit(asyncio.run(main()))


@app.command("runs")
def list_runs(db: Annotated[str, typer.Option()] = DEFAULT_DB) -> None:
    """List runs in the event store."""

    async def main() -> int:
        store = SQLiteEventStore(db)
        await store.open()
        try:
            rows = await store.list_runs()
            if not rows:
                _echo("no runs recorded")
                return 0
            _echo(f"  {'RUN ID':<20} {'EVENTS':>7}  UPDATED")
            for row in rows:
                _echo(f"  {row['run_id']:<20} {row['events']:>7}  {row['updated']}")
            return 0
        finally:
            await store.close()

    raise typer.Exit(asyncio.run(main()))


@app.command()
def replay(
    run_id: Annotated[str, typer.Argument(help="Run to replay.")],
    db: Annotated[str, typer.Option()] = DEFAULT_DB,
    policy: Annotated[Path, typer.Option()] = DEFAULT_POLICY,
) -> None:
    """Replay a recorded run and diff it against the original trajectory."""

    async def main() -> int:
        store = SQLiteEventStore(db)
        await store.open()
        try:
            def build(provider: Any) -> AgentRuntime:
                bundle = (
                    PolicyBundle.from_yaml(policy)
                    if policy.exists()
                    else PolicyBundle.baseline(
                        granted=["KNOWLEDGE_READ", "CALC", "WORKSPACE_WRITE"]
                    )
                )
                return AgentRuntime(
                    store=store,
                    gateway=LLMGateway(providers=[provider], ledger=CostLedger()),
                    registry=build_default_registry(),
                    policy=PolicyEngine(bundle),
                    config=RuntimeConfig(),
                )

            diff = await replay_run(run_id, store=store, build_runtime=build)
            _echo("\n  " + diff.render().replace("\n", "\n  ") + "\n")
            return 0 if diff.identical else 1
        finally:
            await store.close()

    raise typer.Exit(asyncio.run(main()))


@app.command()
def bench(
    trials: Annotated[int, typer.Option(help="Trials per task per fault class.")] = 3,
    seed: Annotated[int, typer.Option()] = 1729,
    out: Annotated[Path, typer.Option(help="Directory for the report.")] = Path("reports"),
) -> None:
    """Run the failure-injection benchmark and write a report."""

    async def main() -> int:
        runner = BenchmarkRunner(trials=trials, seed=seed)
        _echo(f"  running {len(runner.tasks)} tasks x {len(runner.faults)} faults "
              f"x {trials} trials ...")
        report = await runner.run()
        json_path, md_path = report.write(out)
        _echo("")
        _echo(report.to_markdown())
        _echo(f"  wrote {json_path}")
        _echo(f"  wrote {md_path}")
        return 0

    raise typer.Exit(asyncio.run(main()))


@app.command()
def policy_show(
    policy: Annotated[Path, typer.Option()] = DEFAULT_POLICY,
) -> None:
    """Show the active policy bundle: what is granted, and what is not."""
    bundle = PolicyBundle.from_yaml(policy)
    _echo(f"\n  bundle  : {bundle.version}")
    _echo(f"  budget  : ${bundle.budget.max_usd:.2f} / {bundle.budget.max_tokens} tokens "
          f"/ {bundle.budget.max_steps} steps")
    _echo("\n  capabilities:")
    for name, grant in sorted(bundle.capabilities.items()):
        mark = "granted" if grant.granted else "DENIED "
        approval = "  (requires approval)" if grant.requires_approval else ""
        effects = ",".join(sorted(e.value for e in grant.allowed_effects))
        _echo(f"    [{mark}] {name:<22} {effects}{approval}")
    _echo("")


@app.command()
def prune(
    older_than_days: Annotated[float, typer.Option(help="Age cutoff in days.")] = 30.0,
    db: Annotated[str, typer.Option()] = DEFAULT_DB,
    include_unfinished: Annotated[
        bool,
        typer.Option(help="Also delete runs that never finished - they may be recoverable."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete finished runs older than a cutoff. Whole runs only.

    An append-only log grows forever unless something removes from it. This is
    the only operation in FORGE that destroys history, so it asks first.
    """

    async def main() -> int:
        store = SQLiteEventStore(db)
        await store.open()
        try:
            if not yes:
                scope = "finished and unfinished" if include_unfinished else "finished"
                _echo(
                    f"\n  about to delete {scope} runs older than "
                    f"{older_than_days:g} days from {db}"
                )
                typer.confirm("  proceed?", abort=True)
            removed = await store.prune(
                older_than_days=older_than_days,
                keep_unfinished=not include_unfinished,
            )
            _echo(f"\n  pruned {removed} run(s)\n")
            return 0
        finally:
            await store.close()

    raise typer.Exit(asyncio.run(main()))


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8080,
    reload: Annotated[bool, typer.Option(help="Reload on code change (development).")] = False,
) -> None:
    """Run the HTTP service. Requires the `api` extra.

    Binds to loopback by default: exposing an agent runtime on 0.0.0.0 should
    be a deliberate act, not a default.
    """
    try:
        import uvicorn
    except ImportError:
        _echo('\n  the api extra is not installed:  pip install "forge-runtime[api]"\n')
        raise typer.Exit(2) from None

    uvicorn.run(
        "forge.api:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        timeout_graceful_shutdown=30,
    )


@app.command()
def doctor() -> None:
    """Check the local environment: providers, models, and the event store."""

    async def main() -> int:
        _echo("")
        ollama = OllamaProvider()
        healthy = await ollama.healthy()
        await ollama.aclose()
        _echo(f"  ollama at {ollama.host:<28} {'reachable' if healthy else 'not reachable'}")
        _echo("  mock provider                      always available")

        store = SQLiteEventStore(DEFAULT_DB)
        await store.open()
        runs = await store.list_runs(limit=1000)
        await store.close()
        _echo(f"  event store {DEFAULT_DB:<26} {len(runs)} run(s)")

        registry = build_default_registry()
        _echo(f"  tools registered                   {len(registry.names())}")

        bundle = PolicyBundle.from_yaml(DEFAULT_POLICY)
        granted = sum(1 for g in bundle.capabilities.values() if g.granted)
        _echo(f"  policy bundle                      {bundle.version}")
        _echo(f"  capabilities granted               {granted}/{len(bundle.capabilities)}")
        _echo(f"  spend ceiling                      ${bundle.budget.max_usd:.2f} per run")

        try:
            cases = CaseSet.load("cases")
            _echo(f"  case set                           {cases.version} "
                  f"({len(cases)} cases)")
        except (CaseSetError, OSError):
            _echo("  case set                           not found in ./cases")
        _echo("")
        return 0

    raise typer.Exit(asyncio.run(main()))


@app.command()
def demo(
    db: Annotated[str, typer.Option()] = ".forge/demo.db",
) -> None:
    """The full story: run, crash, resume, prove no duplicate effects."""

    # Filesystem setup happens before the loop starts: blocking I/O inside an
    # async function would stall the event loop, and this is not async work.
    path = Path(db)
    if path.exists():
        path.unlink()

    async def main() -> int:
        store = SQLiteEventStore(db)
        await store.open()
        try:
            _echo("\n  [1/4] starting a run that will be killed mid-flight ...")
            runtime = _runtime(
                store, provider_name="mock", model="", policy_path=DEFAULT_POLICY, crash_at=3
            )
            spec = TaskSpec(goal="Summarise what FORGE measured about checkpointing.",
                            tools=DEFAULT_TOOLS)
            run_id = ""
            try:
                await runtime.start(spec)
            except SimulatedCrash as crash:
                runs = await store.list_runs(limit=1)
                run_id = str(runs[0]["run_id"])
                _echo(f"        worker died: {crash}")

            events_before = await store.read(run_id)
            ckpt = await store.latest_checkpoint(run_id)
            _echo(f"  [2/4] log holds {len(events_before)} events; "
                  f"checkpoint at step {ckpt.step_index if ckpt else 0}")

            _echo("  [3/4] restarting a fresh worker and resuming ...")
            fresh = _runtime(store, provider_name="mock", model="", policy_path=DEFAULT_POLICY)
            result = await fresh.resume(run_id)

            effects = [e for e in await store.read(run_id)
                       if e.type.value in ("EFFECT_OBSERVED", "EFFECT_REUSED")]
            observed = [e for e in effects if e.type.value == "EFFECT_OBSERVED"]
            reused = [e for e in effects if e.type.value == "EFFECT_REUSED"]
            keys = [e.idempotency_key for e in observed]

            _echo(f"  [4/4] status={result.status.value} steps={result.steps}")
            _echo("")
            _echo(f"        effects performed   : {len(observed)}")
            _echo(f"        effects reused      : {len(reused)}  (suppressed on resume)")
            _echo(f"        duplicate effects   : {result.duplicate_effects}")
            _echo(f"        unique idempotency  : {len(set(keys))} of {len(keys)}")
            _echo(f"        cost                : ${result.usage.usd:.4f}")
            _echo("")
            if result.answer:
                _echo(f"        {result.answer}")
            _echo("")
            _echo(f"  inspect it:  forge trace {run_id} --db {db}")
            _echo("")
            return 0 if result.ok and result.duplicate_effects == 0 else 1
        finally:
            await store.close()

    raise typer.Exit(asyncio.run(main()))


if __name__ == "__main__":
    app()
