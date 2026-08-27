"""`forge code` - run a coding task against this repository.

    forge code "add a --verbose flag to the CLI"
    forge code "fix the failing test in tests/test_parser.py" --show-diff
    forge code review          # what did the last run change?
    forge code discard         # throw the last run away
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import typer

from forge.coding.agent import CodingAgent
from forge.coding.git import GitRepo, NotARepository
from forge.coding.workspace import Workspace
from forge.core.enums import EventType
from forge.errors import ForgeError

app = typer.Typer(add_completion=False, help="Edit code under runtime control.")


def _echo(text: str = "") -> None:
    typer.echo(text)


def _progress(event: EventType, payload: dict[str, Any]) -> None:
    """Live narration. Shows what the agent is doing, one line per action."""
    match event:
        case EventType.PROPOSAL_RECEIVED:
            tool = payload.get("tool")
            if tool:
                args = payload.get("arguments") or {}
                detail = args.get("path") or args.get("pattern") or args.get("target") or ""
                _echo(f"    {tool:<14} {str(detail)[:60]}")
            else:
                _echo("    answering")
        case EventType.POLICY_DECIDED if payload.get("decision") == "DENY":
            _echo(f"    REFUSED        {payload.get('tool')}: {payload.get('reason')}")
        case EventType.EFFECT_OBSERVED if not payload.get("ok"):
            _echo(f"                   failed: {str(payload.get('error'))[:70]}")
        case EventType.RETRY_SCHEDULED:
            _echo(f"                   retrying ({payload.get('where', 'tool')})")
        case _:
            pass


@app.callback(invoke_without_command=True)
def code(
    ctx: typer.Context,
    task: Annotated[str, typer.Argument(help="What to do.")] = "",
    repo: Annotated[Path, typer.Option(help="Repository root.")] = Path("."),
    max_steps: Annotated[int, typer.Option(help="Step ceiling.")] = 0,
    allow_dirty: Annotated[
        bool, typer.Option(help="Start even with uncommitted changes.")
    ] = False,
    show_diff: Annotated[bool, typer.Option(help="Print the diff when done.")] = False,
    quiet: Annotated[bool, typer.Option(help="Suppress live progress.")] = False,
) -> None:
    """Run a coding task. Edits land on a scratch branch, never on yours."""
    if ctx.invoked_subcommand is not None:
        return
    if not task.strip():
        _echo('\n  usage: forge code "what you want done"\n')
        raise typer.Exit(2)

    async def main() -> int:
        try:
            agent = CodingAgent(repo, allow_dirty=allow_dirty)
        except (NotARepository, ForgeError) as exc:
            _echo(f"\n  {exc}\n")
            return 2

        _echo(f"\n  repo   {agent.workspace.root}")
        _echo(f"  task   {task}")
        _echo("")

        try:
            result = await agent.run(
                task,
                max_steps=max_steps or None,
                on_step=None if quiet else _progress,
            )
        except ForgeError as exc:
            _echo(f"\n  {exc}\n")
            return 2

        _echo("")
        _echo(f"  status         {result.run.status.value}")
        _echo(f"  steps          {result.run.steps}")
        _echo(f"  tokens         {result.run.usage.total_tokens}")
        _echo(f"  branch         {result.branch}")
        _echo(f"  commits        {result.commits}")
        if result.files_touched:
            _echo(f"  files          {', '.join(result.files_touched[:6])}"
                  + (f" (+{len(result.files_touched) - 6})"
                     if len(result.files_touched) > 6 else ""))
        if result.tests_run:
            verdict = "passing" if result.tests_passed else "FAILING"
            _echo(f"  tests          ran {result.tests_run}x, {verdict}")
        if result.run.denials:
            _echo(f"  refused        {len(result.run.denials)} action(s) by policy")
            for denial in result.run.denials[:3]:
                _echo(f"                 {denial['reason']}")
        if result.diff_stat:
            _echo("")
            for line in result.diff_stat.splitlines()[-8:]:
                _echo(f"  {line}")

        if result.run.answer:
            _echo(f"\n  {result.run.answer}\n")
        if result.run.error:
            _echo(f"\n  error: {result.run.error}\n")

        if show_diff and result.changed_anything:
            _echo("  " + "-" * 60)
            _echo(agent.repo.diff(base=result.base_ref))

        if result.changed_anything:
            _echo("\n  review:")
            _echo(result.review_hint)
        else:
            _echo("\n  nothing changed")
        _echo(f"\n  trace: forge trace {result.run.run_id}\n")

        # Exit non-zero if the agent changed code and left the tests failing:
        # that is the state a caller most needs to notice.
        if result.tests_passed is False:
            return 1
        return 0 if result.ok else 1

    raise typer.Exit(asyncio.run(main()))


@app.command("review")
def review(
    repo: Annotated[Path, typer.Option(help="Repository root.")] = Path("."),
    diff: Annotated[bool, typer.Option(help="Show the full diff.")] = False,
) -> None:
    """Show what the most recent agent branch changed."""
    workspace = Workspace(repo)
    git = GitRepo(workspace.root)
    git.require_repo()

    branches = [
        b.strip().lstrip("* ").strip()
        for b in git.run("branch", "--list", "forge/*").splitlines()
        if b.strip()
    ]
    if not branches:
        _echo("\n  no agent branches in this repository\n")
        raise typer.Exit(0)

    latest = branches[-1]
    merge_base = git.run("merge-base", "HEAD", latest).strip()
    _echo(f"\n  branch  {latest}")
    _echo(f"  base    {merge_base[:8]}\n")
    _echo(git.run("log", "--oneline", f"{merge_base}..{latest}"))
    _echo(git.diff_stat(merge_base))
    if diff:
        _echo(git.run("diff", f"{merge_base}..{latest}"))
    _echo(f"\n  keep:     git merge {latest}")
    _echo(f"  discard:  git branch -D {latest}\n")


@app.command("discard")
def discard(
    repo: Annotated[Path, typer.Option(help="Repository root.")] = Path("."),
    branch: Annotated[str, typer.Option(help="Branch to delete. Default: the latest.")] = "",
    yes: Annotated[bool, typer.Option("--yes", help="Skip confirmation.")] = False,
) -> None:
    """Delete an agent branch. Your work is untouched."""
    workspace = Workspace(repo)
    git = GitRepo(workspace.root)
    git.require_repo()

    target = branch
    if not target:
        branches = [
            b.strip().lstrip("* ").strip()
            for b in git.run("branch", "--list", "forge/*").splitlines()
            if b.strip()
        ]
        if not branches:
            _echo("\n  no agent branches to discard\n")
            raise typer.Exit(0)
        target = branches[-1]

    if git.current_branch() == target:
        _echo(f"\n  you are on {target}; check out another branch first\n")
        raise typer.Exit(2)

    if not yes:
        typer.confirm(f"  delete {target}?", abort=True)
    git.delete_branch(target)
    _echo(f"\n  deleted {target}\n")
