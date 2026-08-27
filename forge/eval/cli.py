"""`forge eval` - run a case set against a target.

    forge eval run cases/ --target inprocess
    forge eval run cases/ --target http --base-url http://localhost:8080
    forge eval list cases/
    forge eval compare reports/main reports/pr-482
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import typer

from forge.eval.cases import CaseSet, CaseSetError
from forge.eval.outcomes import Outcome
from forge.eval.report import render_markdown, render_terminal
from forge.eval.results import ResultSet
from forge.eval.runner import Harness, HarnessConfig
from forge.eval.targets import CliTarget, HttpTarget, InProcessTarget, Target

app = typer.Typer(add_completion=False, help="Run case sets against a target.")


def _echo(text: str = "") -> None:
    typer.echo(text)


def _build_target(kind: str, *, base_url: str, fixtures: Path) -> Target:
    match kind:
        case "inprocess":
            return InProcessTarget(fixtures_dir=fixtures)
        case "http":
            return HttpTarget(base_url)
        case "cli":
            return CliTarget()
        case "coding":
            from forge.eval.coding_target import CodingTarget

            return CodingTarget()
        case _:
            raise typer.BadParameter(f"unknown target {kind!r}; expected inprocess, coding, http or cli")


@app.command("run")
def run_cases(
    path: Annotated[Path, typer.Argument(help="Case file or directory.")] = Path("cases"),
    target: Annotated[str, typer.Option(help="inprocess | coding | http | cli")] = "inprocess",
    base_url: Annotated[str, typer.Option(help="Base URL when target=http.")] = "http://127.0.0.1:8080",
    fixtures: Annotated[Path, typer.Option(help="Fixture directory.")] = Path("cases/fixtures"),
    out: Annotated[Path, typer.Option(help="Where to write records + manifest.")] = Path("reports/eval"),
    tags: Annotated[str, typer.Option(help="Only cases with these tags (comma-separated).")] = "",
    ids: Annotated[str, typer.Option(help="Only these case ids (comma-separated).")] = "",
    concurrency: Annotated[int, typer.Option(min=1, max=64)] = 4,
    fail_fast: Annotated[bool, typer.Option(help="Stop after the first failure.")] = False,
    markdown: Annotated[bool, typer.Option(help="Also write report.md.")] = True,
) -> None:
    """Execute a case set and write structured results."""

    async def main() -> int:
        try:
            cases = CaseSet.load(path)
        except CaseSetError as exc:
            _echo(f"\n  case set is invalid: {exc}\n")
            return 2

        cases = cases.select(
            ids=[i for i in ids.split(",") if i] or None,
            tags=[t for t in tags.split(",") if t] or None,
        )
        if not len(cases):
            _echo("\n  no cases selected\n")
            return 2

        sut = _build_target(target, base_url=base_url, fixtures=fixtures)
        _echo(f"\n  {len(cases)} case(s) · set {cases.version} · target {sut.name}")

        def progress(record: Any) -> None:
            mark = {
                Outcome.PASSED.value: "  ok  ",
                Outcome.ASSERTION_FAILED.value: " FAIL ",
                Outcome.SKIPPED.value: " skip ",
            }.get(record.outcome, " !!!! ")
            _echo(f"  {mark} {record.case_id}")

        harness = Harness(
            cases, sut,
            config=HarnessConfig(
                concurrency=concurrency, fail_fast=fail_fast, on_result=progress
            ),
        )
        results = await harness.run()

        paths = results.write(out)
        if markdown:
            (Path(out) / "report.md").write_text(render_markdown(results), encoding="utf-8")

        _echo(render_terminal(results))
        _echo(f"  records:  {paths['records']}")
        _echo(f"  manifest: {paths['manifest']}\n")

        # Exit codes are distinct so CI can react differently: a wrong answer
        # is a product problem, a broken harness or unreachable target is not.
        counts = results.counts()
        if counts.get(Outcome.HARNESS_ERROR.value):
            return 3
        if counts.get(Outcome.TARGET_UNAVAILABLE.value) or counts.get(Outcome.INFRA_ERROR.value):
            return 4
        return 0 if results.green else 1

    raise typer.Exit(asyncio.run(main()))


@app.command("list")
def list_cases(
    path: Annotated[Path, typer.Argument()] = Path("cases"),
) -> None:
    """Show the cases in a set, with their assertions."""
    try:
        cases = CaseSet.load(path)
    except CaseSetError as exc:
        _echo(f"\n  case set is invalid: {exc}\n")
        raise typer.Exit(2) from None

    _echo(f"\n  {cases.suite}  ·  version {cases.version}  ·  {len(cases)} cases")
    _echo(f"  source: {cases.source}\n")
    for case in cases:
        tags = f"  [{','.join(case.tags)}]" if case.tags else ""
        skip = "  (SKIPPED)" if case.skip else ""
        _echo(f"    {case.id}{tags}{skip}")
        for spec in case.expect:
            _echo(f"        expect {spec.get('type')}: {spec.get('value')}")
    _echo("")


@app.command("validate")
def validate(
    path: Annotated[Path, typer.Argument()] = Path("cases"),
) -> None:
    """Check a case set loads, ids are unique and the version is well-formed."""
    try:
        cases = CaseSet.load(path)
    except CaseSetError as exc:
        _echo(f"\n  INVALID: {exc}\n")
        raise typer.Exit(1) from None

    from forge.eval.graders import GRADERS

    problems: list[str] = []
    for case in cases:
        if not case.expect:
            problems.append(f"{case.id}: declares no expectations")
        for spec in case.expect:
            kind = spec.get("type")
            if kind not in GRADERS:
                problems.append(f"{case.id}: unknown grader {kind!r}")

    if problems:
        _echo("")
        for problem in problems:
            _echo(f"  INVALID  {problem}")
        _echo("")
        raise typer.Exit(1)

    _echo(f"\n  valid: {len(cases)} cases, version {cases.version}\n")


@app.command("compare")
def compare(
    baseline: Annotated[Path, typer.Argument(help="Baseline results directory.")],
    current: Annotated[Path, typer.Argument(help="Current results directory.")],
) -> None:
    """Diff two runs. Regressions exit non-zero."""
    before, after = ResultSet.read(baseline), ResultSet.read(current)
    diff = after.compare(before)

    if "incomparable" in diff:
        _echo(f"\n  {diff['incomparable'][0]}")
        _echo("  A verdict is only meaningful as (case-set version x target version).\n")
        raise typer.Exit(2)

    _echo(f"\n  case set {after.manifest.case_set_version}")
    _echo(f"  {before.manifest.target_version}  ->  {after.manifest.target_version}\n")
    for label, ids in diff.items():
        if ids:
            _echo(f"  {label}: {len(ids)}")
            for case_id in ids:
                _echo(f"      {case_id}")
    if not any(diff.values()):
        _echo("  no change")
    _echo("")
    raise typer.Exit(1 if diff.get("regressed") else 0)
