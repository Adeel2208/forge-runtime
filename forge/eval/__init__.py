"""The evaluation harness.

Three things stay separate, on purpose:

    the target     `forge.eval.targets`  - the system under test, behind one adapter interface
    the harness    `forge.eval.runner`   - orchestration: how to run, never what is correct
    the assertions `forge.eval.graders`  - what "correct" means, selected by the case data

Cases are data (`forge.eval.cases`), results are records (`forge.eval.results`),
and failure classes are distinguished (`forge.eval.outcomes`) so infrastructure
noise never masquerades as a verdict on the target.

    from forge.eval import CaseSet, Harness, InProcessTarget

    cases = CaseSet.load("cases/")
    results = await Harness(cases, InProcessTarget()).run()
    results.write("reports/latest")
"""

from __future__ import annotations

from forge.eval.cases import Case, CaseSet, CaseSetError
from forge.eval.coding_target import CodingTarget
from forge.eval.graders import Grade, Grader, build_grader, register_grader
from forge.eval.outcomes import Outcome
from forge.eval.report import render_markdown, render_terminal
from forge.eval.results import CaseRecord, ResultSet, RunManifest
from forge.eval.runner import Harness, HarnessConfig
from forge.eval.targets import (
    CallableTarget,
    CliTarget,
    HttpTarget,
    InProcessTarget,
    Observation,
    Target,
    TargetUnavailable,
)

__all__ = [  # noqa: RUF022 - grouped by concern, not alphabetised
    # cases
    "Case",
    "CaseSet",
    "CaseSetError",
    # harness
    "Harness",
    "HarnessConfig",
    # targets
    "Target",
    "Observation",
    "TargetUnavailable",
    "InProcessTarget",
    "HttpTarget",
    "CliTarget",
    "CodingTarget",
    "CallableTarget",
    # grading
    "Grader",
    "Grade",
    "build_grader",
    "register_grader",
    # results
    "Outcome",
    "CaseRecord",
    "ResultSet",
    "RunManifest",
    "render_markdown",
    "render_terminal",
]
