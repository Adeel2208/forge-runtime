"""Human-readable rendering, derived from records.

Reports are a projection of the evidence, never a substitute for it. Anything
shown here must be recomputable from `records.jsonl` alone.
"""

from __future__ import annotations

from forge.eval.outcomes import Outcome
from forge.eval.results import ResultSet

__all__ = ["render_markdown", "render_terminal"]

_ORDER = [
    Outcome.PASSED, Outcome.ASSERTION_FAILED, Outcome.TIMEOUT,
    Outcome.TARGET_UNAVAILABLE, Outcome.INFRA_ERROR, Outcome.HARNESS_ERROR,
    Outcome.SKIPPED,
]


def render_markdown(results: ResultSet) -> str:
    m = results.manifest
    counts = results.counts()
    judged = results.verdicts

    lines = [
        f"# {m.suite} - evaluation report",
        "",
        f"- case set: `{m.case_set_version}` (`{m.case_set_source}`)",
        f"- target: `{m.target_name}` @ `{m.target_version}`",
        f"- harness: `{m.harness_version}` · seed `{m.seed}` · python `{m.python}`",
        f"- started: {m.started_at}",
        "",
        f"**{sum(1 for r in judged if r.passed)} / {len(judged)} passed** "
        f"({results.pass_rate():.0%} of {len(judged)} judged cases, "
        f"{m.total} executed)",
        "",
        "| Outcome | Count | Means |",
        "|---|---:|---|",
    ]
    meanings = {
        Outcome.PASSED: "every assertion held",
        Outcome.ASSERTION_FAILED: "the target ran and was wrong",
        Outcome.TIMEOUT: "the target did not finish in time",
        Outcome.TARGET_UNAVAILABLE: "could not reach the target (not a verdict)",
        Outcome.INFRA_ERROR: "harness plumbing flaked (not a verdict)",
        Outcome.HARNESS_ERROR: "**bug in the harness itself**",
        Outcome.SKIPPED: "deliberately not run",
    }
    for outcome in _ORDER:
        n = counts.get(outcome.value, 0)
        if n:
            lines.append(f"| `{outcome.value}` | {n} | {meanings[outcome]} |")

    failures = results.failures()
    if failures:
        lines += ["", "## Failed assertions", ""]
        for record in failures:
            lines.append(f"### `{record.case_id}`")
            lines.append("")
            lines.append(f"> {record.input.get('goal', '')}")
            lines.append("")
            for grade in record.grades:
                mark = "PASS" if grade["passed"] else "FAIL"
                note = "" if grade.get("applicable", True) else " _(could not run)_"
                lines.append(f"- **{mark}** `{grade['kind']}` — {grade['reason']}{note}")
                if grade.get("reasoning"):
                    lines.append(f"  - judge: _{grade['reasoning']}_")
            answer = (record.output or {}).get("answer")
            if answer:
                lines += ["", "```", str(answer)[:600], "```"]
            lines.append("")

    broken = [r for r in results.records if r.outcome == Outcome.HARNESS_ERROR.value]
    if broken:
        lines += ["", "## Harness errors", "",
                  "These are bugs in the harness, not verdicts on the target.", ""]
        for record in broken:
            lines.append(f"- `{record.case_id}`: {record.error}")

    lines += ["", "---", "",
              "Records: `records.jsonl` · Manifest: `manifest.json`. "
              "A result is interpretable only as (case-set version x target version)."]
    return "\n".join(lines) + "\n"


def render_terminal(results: ResultSet, *, verbose: bool = False) -> str:
    m = results.manifest
    counts = results.counts()
    judged = results.verdicts
    out = [
        "",
        f"  {m.suite}  case-set {m.case_set_version}  ->  "
        f"{m.target_name} @ {m.target_version}",
        "",
    ]
    for outcome in _ORDER:
        n = counts.get(outcome.value, 0)
        if n:
            out.append(f"    {outcome.value:<20} {n:>4}")
    out += [
        "",
        f"    {sum(1 for r in judged if r.passed)}/{len(judged)} judged cases passed "
        f"({results.pass_rate():.0%})",
    ]
    if verbose or results.failures():
        for record in results.failures():
            out.append(f"      FAIL  {record.case_id}: {record.error}")
    broken = [r for r in results.records if r.outcome == Outcome.HARNESS_ERROR.value]
    for record in broken:
        out.append(f"      HARNESS BUG  {record.case_id}: {record.error}")
    out.append("")
    return "\n".join(out)
