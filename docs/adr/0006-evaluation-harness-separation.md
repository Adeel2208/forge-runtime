# ADR-0006: The harness orchestrates; it does not know what "correct" means

- **Status:** accepted
- **Date:** 2026-08-26
- **Supersedes:** the `BenchmarkRunner` in `forge/evaluation/benchmark.py`

## Context

The first evaluation implementation put task definitions, expected answers,
fault configuration and grading logic all inside `benchmark.py`. It worked,
and it was already wrong in a way that compounds:

- Adding one new check meant editing the runner.
- A domain expert who understood the acceptance criteria but not Python could
  not add a case.
- The task list was a Python literal, so it was not diffable as *content*, and
  a reviewer could not see "what are we actually testing for?" without reading
  an engine.
- Every failure was a boolean. An unreachable service and a wrong answer
  produced the same red.

The last point is the one that destroys a suite's credibility. Once people
learn that red sometimes means "the network was flaky", they stop treating red
as information.

## Decision

Three things are separated, and the separation is enforced by module
boundaries rather than convention:

| Concern | Module | Knows about |
|---|---|---|
| System under test | `forge.eval.targets` | one `Target` interface, several drivers |
| Orchestration | `forge.eval.runner` | how to execute, retry, isolate, record |
| Assertions | `forge.eval.graders` | what "correct" means |

Supporting decisions that fall out of it:

**Cases are data.** `cases/*.yaml`, loaded by `forge.eval.cases`. Stable ids,
validated at load time. Adding coverage is a YAML edit.

**Targets are adapters.** `InProcessTarget`, `HttpTarget`, `CliTarget`,
`CallableTarget`. The same case set runs against a library, a deployed
service, or a shipped binary with `--target`.

**Failure classes are a closed vocabulary.** `PASSED`, `ASSERTION_FAILED`,
`TARGET_UNAVAILABLE`, `INFRA_ERROR`, `HARNESS_ERROR`, `TIMEOUT`, `SKIPPED`.
`Outcome.is_target_verdict` decides what counts toward a pass rate, so infra
noise cannot masquerade as a quality regression. The CLI exit code follows:
`1` for a product regression, `4` for infrastructure, `3` for a harness bug.

**Retries are for infrastructure only.** `Outcome.retryable` gates them.
Re-running a failed assertion is sampling until you like the answer.

**Results are records.** JSONL plus a manifest, carrying case-set version,
target version, seed, timings, cost, raw output and every grade with its
reasoning. Reports are a projection; the evidence is the record.

**Case sets are versioned.** A verdict is only interpretable as
(case-set version x target version). `ResultSet.compare` refuses to diff runs
from different case-set versions rather than producing a misleading number.

## The negative fixtures

`tests/eval/test_harness_meta.py` exists because a suite that would stay green
against a broken build is worse than no suite. It asserts the harness fails
against targets that are deliberately wrong, empty, unreachable, slow, or
that raise unclassified exceptions - and that each produces the *correct*
failure class, not merely a failure.

It also pins two behaviours that are easy to regress into silent passes:

- a grader whose precondition is missing (no trajectory available) reports
  `applicable=False` and **fails**, rather than passing on a check it did not run;
- a case that declares no expectations **fails**, because a case that asserts
  nothing cannot be evidence of anything.

## Consequences

**Good.** New checks are a grader plus a line of YAML. New targets are one
adapter. Neither touches the runner.

**Good.** The case set is reviewable by the people who own the acceptance
criteria, in a pull request, as content.

**Good.** Trend analysis and regression gating are possible, because records
carry both versions and the pass rate is computed only over target verdicts.

**Costs.** More moving parts than a single benchmark file, and one more
interface to learn. Justified once a second target or a second grader exists -
and both existed within a day.

**Costs.** The old `BenchmarkRunner` still exists for failure-injection
sweeps, which are a different shape of question (a matrix over fault classes
rather than a pass/fail suite). It should eventually be expressed as a case
set dimension; until then there are two evaluation entry points, which is one
too many.
