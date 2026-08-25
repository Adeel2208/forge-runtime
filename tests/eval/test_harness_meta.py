"""Negative fixtures: the harness must fail against deliberately broken targets.

A green suite that would stay green against a non-compliant build is worse
than no suite, because it converts absence of signal into false confidence.
So every target here is broken on purpose, and each test asserts the harness
*notices* - and notices with the right failure class.

These are the tests that make the other tests worth trusting.
"""

from __future__ import annotations

import pytest

from forge.eval import (
    CallableTarget,
    CaseSet,
    Harness,
    HarnessConfig,
    Observation,
    Outcome,
    TargetUnavailable,
)
from tests.conftest import run

CASES = {
    "version": "1.0.0",
    "suite": "meta",
    "cases": [
        {
            "id": "meta.must-say-hello",
            "goal": "Say hello",
            "expect": [{"contains": "hello"}, {"terminal_status": "COMPLETED"}],
        }
    ],
}


def _case_set(**overrides) -> CaseSet:  # noqa: ANN003
    return CaseSet.from_dict({**CASES, **overrides}, source="<meta>")


def _target(fn, **kw):  # noqa: ANN001, ANN003
    return CallableTarget(fn, **kw)


# ── the harness must FAIL a wrong target ────────────────────────────────


def test_wrong_answer_is_an_assertion_failure() -> None:
    """The headline negative fixture: a target that answers incorrectly."""

    async def wrong(case, seed):  # noqa: ANN001
        return Observation(answer="goodbye", status="COMPLETED")

    results = run(Harness(_case_set(), _target(wrong, name="wrong")).run())
    record = results.records[0]

    assert record.outcome == Outcome.ASSERTION_FAILED.value
    assert not results.green, "a suite must not be green against a wrong target"
    assert results.pass_rate() == 0.0
    assert any(not g["passed"] for g in record.grades)


def test_a_target_that_answers_nothing_fails() -> None:
    async def empty(case, seed):  # noqa: ANN001
        return Observation(answer=None, status="COMPLETED")

    results = run(Harness(_case_set(), _target(empty, name="empty")).run())
    assert results.records[0].outcome == Outcome.ASSERTION_FAILED.value


def test_a_target_that_never_terminates_correctly_fails() -> None:
    """Right text, wrong terminal state. Both assertions must be evaluated."""

    async def half_right(case, seed):  # noqa: ANN001
        return Observation(answer="hello there", status="FAILED")

    results = run(Harness(_case_set(), _target(half_right, name="half")).run())
    record = results.records[0]
    assert record.outcome == Outcome.ASSERTION_FAILED.value
    kinds = {g["kind"]: g["passed"] for g in record.grades}
    assert kinds["contains"] is True
    assert kinds["terminal_status"] is False


def test_a_case_with_no_expectations_cannot_pass() -> None:
    """An assertion-free case is a hole in the suite, not a free pass."""

    async def anything(case, seed):  # noqa: ANN001
        return Observation(answer="whatever", status="COMPLETED")

    bare = CaseSet.from_dict(
        {"version": "1.0.0", "suite": "meta",
         "cases": [{"id": "meta.no-expectations", "goal": "x"}]},
        source="<meta>",
    )
    results = run(Harness(bare, _target(anything)).run())
    assert results.records[0].outcome == Outcome.ASSERTION_FAILED.value


# ── failure classes must stay distinct ──────────────────────────────────


def test_unreachable_target_is_not_an_assertion_failure() -> None:
    """Infra is not a verdict. This distinction is the whole point."""

    async def unreachable(case, seed):  # noqa: ANN001
        raise TargetUnavailable("connection refused")

    results = run(
        Harness(
            _case_set(), _target(unreachable, name="down"),
            config=HarnessConfig(max_infra_retries=1, retry_backoff_s=0.0),
        ).run()
    )
    record = results.records[0]
    assert record.outcome == Outcome.TARGET_UNAVAILABLE.value
    assert record.outcome != Outcome.ASSERTION_FAILED.value
    assert not Outcome(record.outcome).is_target_verdict
    assert results.verdicts == [], "infra noise must be excluded from the pass rate"


def test_an_unclassified_adapter_exception_is_a_harness_bug() -> None:
    """An adapter that raises something it did not classify blames itself."""

    async def buggy(case, seed):  # noqa: ANN001
        raise ValueError("adapter forgot to translate this")

    results = run(Harness(_case_set(), _target(buggy, name="buggy")).run())
    assert results.records[0].outcome == Outcome.HARNESS_ERROR.value


def test_a_broken_grader_is_a_harness_bug_not_a_target_failure() -> None:
    from forge.eval.graders import register_grader

    def explode(value, **kw):  # noqa: ANN001, ANN003
        raise RuntimeError("grader is broken")

    register_grader("exploding", explode)
    cases = _case_set(cases=[{
        "id": "meta.broken-grader", "goal": "x",
        "expect": [{"type": "exploding", "value": 1}],
    }])

    async def fine(case, seed):  # noqa: ANN001
        return Observation(answer="hello", status="COMPLETED")

    results = run(Harness(cases, _target(fine)).run())
    assert results.records[0].outcome == Outcome.HARNESS_ERROR.value


def test_a_slow_target_times_out_rather_than_hanging() -> None:
    import asyncio

    async def slow(case, seed):  # noqa: ANN001
        await asyncio.sleep(5)
        return Observation(answer="hello", status="COMPLETED")

    cases = _case_set(cases=[{
        "id": "meta.slow", "goal": "x", "timeout_s": 0.2,
        "expect": [{"contains": "hello"}],
    }])
    results = run(Harness(cases, _target(slow)).run())
    assert results.records[0].outcome == Outcome.TIMEOUT.value


# ── retries are for infrastructure only ─────────────────────────────────


def test_assertion_failures_are_never_retried() -> None:
    """Re-rolling a failed assertion is sampling until you like the answer."""
    calls = {"n": 0}

    async def wrong(case, seed):  # noqa: ANN001
        calls["n"] += 1
        return Observation(answer="goodbye", status="COMPLETED")

    results = run(
        Harness(
            _case_set(), _target(wrong),
            config=HarnessConfig(max_infra_retries=3, retry_backoff_s=0.0),
        ).run()
    )
    assert calls["n"] == 1, "a failed assertion must be executed exactly once"
    assert results.records[0].attempts == 1


def test_infra_failures_are_retried() -> None:
    calls = {"n": 0}

    async def flaky(case, seed):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] < 3:
            raise TargetUnavailable("still starting up")
        return Observation(answer="hello", status="COMPLETED")

    results = run(
        Harness(
            _case_set(), _target(flaky),
            config=HarnessConfig(max_infra_retries=3, retry_backoff_s=0.0),
        ).run()
    )
    assert results.records[0].outcome == Outcome.PASSED.value
    assert results.records[0].attempts == 3


def test_setup_failure_does_not_look_like_a_suite_of_wrong_answers() -> None:
    class DeadTarget:
        name = "dead"
        version = "0"

        async def available(self) -> bool:
            return False

        async def setup(self) -> None:
            raise TargetUnavailable("service is down")

        async def execute(self, case, *, seed):  # noqa: ANN001, ANN003
            raise AssertionError("must never execute")

        async def teardown(self) -> None:
            return None

    results = run(Harness(_case_set(), DeadTarget()).run())
    assert all(r.outcome == Outcome.TARGET_UNAVAILABLE.value for r in results.records)
    assert results.verdicts == []


# ── graders that cannot run must not pass ───────────────────────────────


def test_a_check_that_cannot_run_fails_rather_than_passing_silently() -> None:
    """No trajectory means the trajectory check did not happen. That is not green."""

    async def no_trajectory(case, seed):  # noqa: ANN001
        return Observation(answer="hello", status="COMPLETED", events=[])

    cases = _case_set(cases=[{
        "id": "meta.needs-trajectory", "goal": "x",
        "expect": [{"tool_used": "search_corpus"}],
    }])
    results = run(Harness(cases, _target(no_trajectory)).run())
    record = results.records[0]
    assert record.outcome == Outcome.ASSERTION_FAILED.value
    assert record.grades[0]["applicable"] is False


# ── skips are reported, never silent ────────────────────────────────────


def test_skipped_cases_are_recorded_and_do_not_confer_green() -> None:
    async def never(case, seed):  # noqa: ANN001
        raise AssertionError("skipped case must not execute")

    cases = _case_set(cases=[{
        "id": "meta.skipped", "goal": "x", "skip": "pending fixture",
        "expect": [{"contains": "hello"}],
    }])
    results = run(Harness(cases, _target(never)).run())
    assert results.records[0].outcome == Outcome.SKIPPED.value
    assert results.records[0].error == "pending fixture"
    assert not results.green, "a suite that only skipped is not passing"
    assert results.pass_rate() == 0.0
