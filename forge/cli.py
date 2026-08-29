"""The `forge` command line.

Every verb here exists to make a spec §27 acceptance criterion checkable by
hand: `run` then `resume` proves crash recovery, `trace` proves the audit
trail, `replay` proves determinism, `bench` produces the report.
"""

from __future__ import annotations

import asyncio
import contextlib
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

# `forge code ...` - the coding agent. Optional import: the runtime does not
# depend on it, and a repo without git should still get the rest of the CLI.
try:
    from forge.coding.cli import app as code_app

    app.add_typer(code_app, name="code")
except ImportError:  # pragma: no cover - defensive
    pass

@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """FORGE - a durable, policy-aware runtime, and a coding agent built on it."""
    if ctx.invoked_subcommand is not None:
        return
    # Bare `forge` opens the interactive session: the first thing someone
    # types should do something useful, not print a usage screen.
    from forge.coding.session import run_session

    raise typer.Exit(asyncio.run(run_session(Path())))


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
def init(
    directory: Annotated[Path, typer.Argument(help="Where to scaffold.")] = Path("."),
    force: Annotated[bool, typer.Option(help="Overwrite existing files.")] = False,
) -> None:
    """Scaffold a working project: config, tools, policy and a case set."""
    from forge.scaffold import scaffold

    result = scaffold(directory, force=force)

    _echo("")
    for path in result.created:
        _echo(f"  created  {path.relative_to(Path(directory).resolve())}")
    for path in result.skipped:
        _echo(f"  exists   {path.relative_to(Path(directory).resolve())}  (left alone)")

    if not result.anything_created:
        _echo("\n  nothing to do - this project is already set up\n")
        raise typer.Exit(0)

    _echo(
        "\n  next:\n"
        "    1. edit tools.py       - replace the examples with what your agent may do\n"
        "    2. edit policy.yaml    - grant the capabilities those tools need\n"
        "    3. edit forge.toml     - point it at your model\n"
        "    4. forge doctor        - check the setup\n"
        "    5. forge run \"...\"     - run a task\n"
        "       forge eval run cases/  - check it behaves\n"
    )


@app.command()
def run(
    goal: Annotated[str, typer.Argument(help="What the agent should accomplish.")],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="forge.toml to load.")
    ] = None,
    tools: Annotated[
        str, typer.Option(help="Comma-separated tool allow-list. Defaults to config.")
    ] = "",
    max_steps: Annotated[int, typer.Option(help="Override the step ceiling.")] = 0,
    approve: Annotated[bool, typer.Option(help="Auto-approve irreversible actions.")] = False,
    quiet: Annotated[bool, typer.Option(help="Suppress live progress.")] = False,
) -> None:
    """Run a task against the configured model and tools.

    Reads `forge.toml` and `FORGE_*`. If no real model is configured this
    refuses rather than returning something that looks like an answer - see
    `forge demo` for the scripted walkthrough.
    """
    from forge.config import ForgeConfig
    from forge.deployment import Forge

    config = ForgeConfig.load(config_path)

    # Refuse to fake it. A CLI that answers convincingly without a model is
    # worse than one that errors, because the user believes it.
    if all(p.kind == "mock" for p in config.providers):
        _echo(
            "\n  no model is configured, so this would return a canned answer.\n\n"
            "  set one up with any of:\n"
            "    forge init                      scaffold forge.toml and edit [[providers]]\n"
            "    FORGE_PROVIDER=ollama FORGE_MODEL=qwen3:8b forge run \"...\"\n"
            "    FORGE_PROVIDER=openai FORGE_MODEL=gpt-4o-mini "
            "FORGE_API_KEY_ENV=OPENAI_API_KEY forge run \"...\"\n\n"
            "  or see the scripted walkthrough:  forge demo\n"
        )
        raise typer.Exit(2)

    async def main() -> int:
        async with Forge(config=config) as forge:
            allow = [t for t in tools.split(",") if t] or list(config.tools)
            if not allow:
                _echo(
                    "\n  no tools are allow-listed, so the agent can only answer from\n"
                    "  what the model already knows. Set `tools` in forge.toml or pass\n"
                    "  --tools to grant some.\n"
                )
            # Mint the id here so the progress tail can follow the run from
            # its first event rather than joining halfway through.
            from forge.ids import new_id

            run_id = new_id("run")
            stop = asyncio.Event()
            watcher = (
                None if quiet else asyncio.create_task(_stream_progress(forge, run_id, stop))
            )
            _echo("")
            try:
                result = await forge.run(
                    goal, tools=allow, max_steps=max_steps or None, run_id=run_id
                )
            finally:
                stop.set()
                if watcher is not None:
                    with contextlib.suppress(Exception):
                        await watcher

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
                _echo(f"  error  : {result.error}\n")
                hint = await _failure_hint(result.error, forge)
                if hint:
                    _echo(f"  fix    : {hint}\n")
            _echo(f"  inspect: forge trace {result.run_id}\n")
            return 0 if result.ok else 1

    del approve  # approval flows through the policy bundle, not a CLI flag
    raise typer.Exit(asyncio.run(main()))


def _progress_line(event: Any) -> str | None:
    """One short line for an event worth watching, or None to stay quiet.

    Only the events that answer "is it still working, and on what". Echoing
    the whole log would be `forge trace`, which is a different command for a
    different question.
    """
    from forge.coding.ui import accent, dim, error, ok, warn

    kind = event.type.value
    p = event.payload
    step = event.step_index or 0

    if kind == "STEP_STARTED":
        return dim(f"  step {step}")
    if kind == "MODEL_CALLED":
        usage = p.get("usage") or {}
        got = usage.get("output_tokens") or 0
        return dim(f"    thought for {got} tokens" if got else "    thinking")
    if kind == "PROPOSAL_RECEIVED":
        return accent(f"    proposes {p['tool']}") if p.get("tool") else dim("    answering")
    if kind == "POLICY_DECIDED" and p.get("decision") == "DENY":
        return warn(f"    refused  {p.get('capability') or ''} - {p.get('reason', '')}")
    if kind == "EFFECT_OBSERVED":
        return (
            ok(f"    ran      {p.get('tool')}")
            if p.get("ok")
            else error(f"    failed   {p.get('tool')}: {str(p.get('error', ''))[:60]}")
        )
    if kind == "EFFECT_REUSED":
        return warn(f"    skipped  {p.get('tool')} - already done, not repeated")
    if kind == "LOOP_DETECTED":
        act = p.get("action")
        return warn(f"    looping  {'warned once' if act == 'warned' else 'halting'}")
    if kind == "RUN_RESUMED":
        return warn("    resumed from checkpoint")
    return None


async def _stream_progress(forge: Any, run_id: str, stop: asyncio.Event) -> None:
    """Render the run as it happens, by tailing its own event log.

    A local model takes half a minute on a two-step task, and the CLI printed
    nothing at all until it finished - which is indistinguishable from a hang,
    and the reasonable response to a hang is Ctrl-C.

    Nothing new is instrumented for this: it tails the same durable log that
    `trace` reads and the console renders. If the display is wrong, the log is
    wrong, and that is worth knowing.
    """
    seen = 0
    while not stop.is_set():
        with contextlib.suppress(Exception):
            for event in await forge.events(run_id, after_seq=seen):
                seen = max(seen, event.seq)
                line = _progress_line(event)
                if line:
                    _echo(line)
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=0.35)


async def _failure_hint(error: str, forge: Any) -> str | None:
    """Turn a run failure into the command that fixes it, where one exists.

    `forge doctor` already says "start it with `ollama serve`", but doctor is
    not what people type - `forge run` is, and it was printing the raw
    provider error. Having the friendlier message live only in the diagnostic
    nobody runs first is the same as not having it.
    """
    lowered = error.lower()
    if "unavailable" not in lowered and "unreachable" not in lowered:
        return None
    for provider in forge._build_providers():
        diagnose = getattr(provider, "diagnose", None)
        if diagnose is None:
            continue
        with contextlib.suppress(Exception):
            detail = await diagnose()
            if detail:
                return str(detail)
    return None


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

    _announce(host, port)
    uvicorn.run(
        "forge.api:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        timeout_graceful_shutdown=30,
    )


def _announce(host: str, port: int) -> None:
    """Say where the console is, and whether anything is guarding it.

    Starting without `FORGE_API_KEYS` silently disabled auth and printed
    nothing, which on a service that executes tools and spends money is a
    footgun rather than a convenience. It stays permitted - it is genuinely
    useful on a laptop - but it is no longer quiet, and binding a keyless
    service to anything other than loopback is refused outright.
    """
    import os

    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    keys = os.environ.get("FORGE_API_KEYS", "").strip()

    _echo("")
    _echo(f"  console   http://{shown}:{port}")
    _echo(f"  api       http://{shown}:{port}/docs")
    if keys:
        _echo("  auth      enabled (FORGE_API_KEYS)")
    else:
        _echo("  auth      DISABLED - anyone who can reach this port can run tools")
        _echo('            set FORGE_API_KEYS="you:some-secret" to require a key')
        if host not in ("127.0.0.1", "localhost", "::1"):
            _echo("")
            _echo(f"  refusing to bind {host} without authentication.")
            raise typer.Exit(2)
    _echo("")


@app.command()
def ui(
    port: Annotated[int, typer.Option(help="Port to serve on.")] = 8080,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open a browser window.")
    ] = True,
) -> None:
    """Open the console: one command, no configuration.

    `serve` is the deployment surface and expects you to bring a key. This is
    the local one - it mints a key for this session, prints it, and opens the
    browser at it. Loopback only, and never silently unauthenticated.
    """
    import os
    import secrets
    import threading
    import webbrowser

    try:
        import uvicorn
    except ImportError:
        _echo('\n  the api extra is not installed:  pip install "forge-runtime[api]"\n')
        raise typer.Exit(2) from None

    if not os.environ.get("FORGE_API_KEYS", "").strip():
        # A fresh secret per session rather than a fixed default: a shipped
        # default key is a shipped vulnerability the moment someone binds this
        # to something other than loopback.
        os.environ["FORGE_API_KEYS"] = f"local:{secrets.token_urlsafe(18)}"
    key = os.environ["FORGE_API_KEYS"].split(":", 1)[1]
    url = f"http://127.0.0.1:{port}"

    _echo("")
    _echo(f"  console   {url}")
    _echo(f"  key       {key}")
    _echo("            paste it into the field at the top right, once")
    _echo("")

    # Serve the workbench when this is a repository root, and open straight
    # into it: someone running `forge ui` inside their code wants the editor,
    # not a list of runs.
    from forge.coding.git import GitRepo

    repo = Path.cwd()
    is_repo_root = False
    with contextlib.suppress(Exception):
        is_repo_root = GitRepo(repo).is_repo_root

    if is_repo_root:
        _echo("  workbench opens on this repository; /  for the run console")
        os.environ["FORGE_REPO"] = str(repo)
        url += "/code"
    else:
        _echo("  not a git repository root - the run console only")
    _echo("")

    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "forge.api:create_app",
        factory=True,
        host="127.0.0.1",
        port=port,
        timeout_graceful_shutdown=30,
    )


def _open_app_window(url: str) -> bool:
    """Open `url` in a chromeless window, so it reads as an application.

    Chromium's `--app=` gives a window with no tabs, address bar or bookmarks -
    which is the whole visual difference between "a website" and "a program",
    without a bundler, a Node toolchain or a 200MB runtime. If no Chromium is
    installed we fall back to an ordinary tab: worse, but never broken.
    """
    import shutil
    import subprocess

    candidates = (
        "chrome", "google-chrome", "google-chrome-stable", "chromium",
        "chromium-browser", "msedge", "brave",
    )
    windows_paths = (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    )
    found = next((shutil.which(c) for c in candidates if shutil.which(c)), None)
    if found is None:
        found = next((p for p in windows_paths if Path(p).exists()), None)
    if found is None:
        return False
    with contextlib.suppress(Exception):
        subprocess.Popen(
            [found, f"--app={url}", "--window-size=1440,900"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    return False


@app.command()
def studio(
    port: Annotated[int, typer.Option(help="Port to serve on.")] = 8080,
    window: Annotated[
        bool, typer.Option("--window/--tab", help="Chromeless window, or a browser tab.")
    ] = True,
    open_browser: Annotated[
        bool,
        typer.Option(
            "--open/--no-open",
            help="Open a window. The desktop app passes --no-open and shows the page itself.",
        ),
    ] = True,
) -> None:
    """Open FORGE Studio - the editor, the agent and the diff in one window.

    This is the whole product in one command: it serves the app, mints a key
    for the session, and opens a window on the repository you are standing in.
    """
    import os
    import secrets
    import threading
    import webbrowser
    from urllib.parse import quote

    try:
        import uvicorn
    except ImportError:
        _echo('\n  the api extra is not installed:  pip install "forge-runtime[api]"\n')
        raise typer.Exit(2) from None

    from forge.coding.git import GitRepo

    repo = Path.cwd()
    is_root = False
    with contextlib.suppress(Exception):
        is_root = GitRepo(repo).is_repo_root
    if not is_root:
        _echo("")
        _echo(f"  {repo} is not the root of a git repository.")
        _echo("  Studio edits and branches a repository, so it needs one:")
        _echo(f"    git init {repo}")
        _echo("  or cd to the root of an existing repository and try again.")
        _echo("")
        raise typer.Exit(2)

    if not os.environ.get("FORGE_API_KEYS", "").strip():
        os.environ["FORGE_API_KEYS"] = f"studio:{secrets.token_urlsafe(18)}"
    token = os.environ["FORGE_API_KEYS"].split(":", 1)[1]
    os.environ["FORGE_REPO"] = str(repo)
    # The key travels in the fragment, which browsers never send to the server.
    # The page consumes it once and strips it from the address, so the window
    # opens signed in rather than asking someone to copy a secret by hand.
    url = f"http://127.0.0.1:{port}/code#key={quote(token)}"

    _echo("")
    _echo(f"  FORGE Studio   {repo.name}")
    _echo(f"  url            {url}")
    _echo(f"  key            {token}")
    _echo("")

    def launch() -> None:
        if not (window and _open_app_window(url)):
            webbrowser.open(url)

    if open_browser:
        threading.Timer(1.5, launch).start()
    uvicorn.run(
        "forge.api:create_app", factory=True, host="127.0.0.1", port=port,
        timeout_graceful_shutdown=30,
    )


@app.command()
def install(
    port: Annotated[int, typer.Option(help="Port the shortcut will serve on.")] = 8080,
) -> None:
    """Add FORGE Studio to this machine's applications.

    Creates a launcher for the repository you are standing in, so Studio opens
    from the Start Menu or Applications rather than from a terminal. Studio
    itself is also installable from its own window - the button appears in the
    title bar - which gives it an icon and a window of its own.
    """
    from forge.coding.git import GitRepo
    from forge.desktop import install_shortcut

    repo = Path.cwd()
    is_root = False
    with contextlib.suppress(Exception):
        is_root = GitRepo(repo).is_repo_root
    if not is_root:
        _echo("")
        _echo(f"  {repo} is not the root of a git repository.")
        _echo("  Studio launches against one repository; cd to its root first.")
        _echo("")
        raise typer.Exit(2)

    entry = install_shortcut(repo, port=port)
    _echo("")
    if entry.created:
        _echo(f"  installed   {entry.path.name}")
        _echo(f"  location    {entry.path.parent}")
        _echo(f"  opens       {entry.repo}")
        _echo("")
        _echo("  Launch it from your applications menu. Inside Studio, the")
        _echo("  Install button gives it its own window and icon.")
    else:
        _echo(f"  could not create a shortcut: {entry.note}")
        _echo(f"  you can still run it directly:  {entry.target}")
    _echo("")


@app.command()
def doctor(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="forge.toml to inspect.")
    ] = None,
) -> None:
    """Report what *this project* is actually configured to do.

    Reads the same config the runtime does, so what it prints is what would
    run. Reporting the packaged defaults while the project uses its own would
    be worse than printing nothing.
    """
    from forge.config import ForgeConfig
    from forge.deployment import Forge

    async def main() -> int:
        config = ForgeConfig.load(config_path)
        problems: list[str] = []
        _echo("")

        # -- config source --------------------------------------------------
        toml = Path(config_path) if config_path else Path("forge.toml")
        if toml.exists():
            _echo(f"  config              {toml}")
        else:
            _echo("  config              none found (using defaults)")
            problems.append("no forge.toml - run `forge init` to scaffold one")

        # -- models ---------------------------------------------------------
        kinds = [p.kind for p in config.providers]
        if all(k == "mock" for k in kinds):
            _echo("  model               none configured (mock only)")
            problems.append(
                "no real model - set [[providers]] in forge.toml, or FORGE_PROVIDER"
            )
        else:
            # Diagnostics must survive a broken setup - that is the whole
            # point of running them. Report the failure, keep checking.
            try:
                async with Forge(config=config) as forge:
                    health = await forge.health()
                for entry in health["providers"]:
                    state = "ready" if entry["healthy"] else "NOT usable"
                    _echo(f"  model               {entry['name']}/{entry['model']}  {state}")
                    if not entry["healthy"]:
                        # Prefer the provider's own diagnosis: it knows whether
                        # the daemon is down or the model was never pulled, and
                        # those need entirely different actions.
                        problems.append(
                            str(entry.get("detail"))
                            if entry.get("detail")
                            else f"provider {entry['name']} is not reachable"
                        )
            except Exception as exc:
                _echo(f"  model               could not start: {exc}")
                problems.append(str(exc))

        # -- tools ----------------------------------------------------------
        try:
            registry = Forge._build_registry(config)
            source = config.tools_module or "bundled examples"
            _echo(f"  tools               {len(registry.names())} from {source}")
            if not config.tools_module:
                problems.append(
                    "using the bundled example tools - set tools_module in forge.toml"
                )
            unknown = [t for t in config.tools if not registry.has(t)]
            if unknown:
                problems.append(f"allow-listed but not registered: {', '.join(unknown)}")
        except Exception as exc:
            _echo(f"  tools               FAILED to load: {exc}")
            problems.append("tools_module could not be imported")

        # -- policy ---------------------------------------------------------
        try:
            bundle = (
                PolicyBundle.from_yaml(config.policy_bundle)
                if config.policy_bundle
                else PolicyBundle.from_yaml(DEFAULT_POLICY)
            )
            granted = sum(1 for g in bundle.capabilities.values() if g.granted)
            where = config.policy_bundle or "packaged default"
            _echo(f"  policy              {bundle.version} from {where}")
            _echo(f"  capabilities        {granted}/{len(bundle.capabilities)} granted")
            _echo(f"  spend ceiling       ${config.budget.max_usd:.2f} per run")
        except Exception as exc:
            _echo(f"  policy              FAILED to load: {exc}")
            problems.append("policy bundle could not be loaded")

        # -- state ----------------------------------------------------------
        store = SQLiteEventStore(config.sqlite_path)
        await store.open()
        runs = await store.list_runs(limit=1000)
        unfinished = await store.unfinished_runs()
        await store.close()
        _echo(f"  event store         {config.sqlite_path}  ({len(runs)} runs)")
        if unfinished:
            _echo(f"  unfinished runs     {len(unfinished)}  (a supervisor would resume these)")

        # -- cases ----------------------------------------------------------
        try:
            cases = CaseSet.load("cases")
            _echo(f"  case set            {cases.version}  ({len(cases)} cases)")
        except (CaseSetError, OSError):
            _echo("  case set            none in ./cases")
            problems.append("no case set - run `forge init`, then `forge eval run cases/`")

        if problems:
            _echo("\n  needs attention:")
            for problem in problems:
                _echo(f"    - {problem}")
            _echo("")
            return 1

        _echo("\n  ready\n")
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
