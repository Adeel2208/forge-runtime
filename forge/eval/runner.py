"""The generic runner.

This module knows how to execute a case against a target, classify what
happened, and hand the observation to graders. It contains no assertion and
no domain knowledge - adding a new kind of check must never require editing
this file. If you find yourself wanting to, add a grader instead.

Three behaviours are load-bearing and easy to get wrong:

* **Retries are for infrastructure only.** `Outcome.retryable` gates them.
  A failed assertion is never re-run; re-rolling until an assertion passes is
  sampling, not testing.
* **Failure classes stay distinct.** An exception from the adapter means the
  target was unreachable; an exception from a grader means the *harness* is
  broken. Collapsing those into "failed" is how a suite stops being believed.
* **Cases are isolated.** Each gets a derived seed and its own hermetic
  fixtures, so results do not depend on execution order.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from forge.eval.cases import Case, CaseSet
from forge.eval.graders import grade_all
from forge.eval.outcomes import Outcome
from forge.eval.results import CaseRecord, ResultSet, RunManifest
from forge.eval.targets import Observation, Target, TargetUnavailable

__all__ = ["Harness", "HarnessConfig"]


@dataclass
class HarnessConfig:
    concurrency: int = 4
    max_infra_retries: int = 2
    """Attempts for TARGET_UNAVAILABLE / INFRA_ERROR only."""

    retry_backoff_s: float = 0.5
    fail_fast: bool = False
    judge_provider: Any = None
    on_result: Callable[[CaseRecord], None] | None = None
    """Progress hook. The runner never prints."""


class Harness:
    """A generic runner over a case set.

        harness = Harness(case_set, target)
        results = await harness.run()

    The harness orchestrates. It does not decide correctness, and it does not
    know what a FORGE run is - only what a `Target` and a `Grade` are.
    """

    def __init__(
        self,
        cases: CaseSet,
        target: Target,
        *,
        config: HarnessConfig | None = None,
    ) -> None:
        self.cases = cases
        self.target = target
        self.config = config or HarnessConfig()

    async def run(self) -> ResultSet:
        from forge import __version__

        manifest = RunManifest(
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
            case_set_version=self.cases.version,
            case_set_source=self.cases.source,
            suite=self.cases.suite,
            target_name=self.target.name,
            target_version="unknown",
            harness_version=__version__,
            seed=self.cases.seed,
        )
        results = ResultSet(manifest=manifest)

        # -- setup. A target that cannot start is not a suite of failures.
        try:
            await self.target.setup()
            manifest.target_version = self.target.version
        except TargetUnavailable as exc:
            manifest.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
            results.records = [
                self._record(
                    case, Outcome.TARGET_UNAVAILABLE, error=f"setup failed: {exc}"
                )
                for case in self.cases
            ]
            return results
        except Exception as exc:
            manifest.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
            results.records = [
                self._record(case, Outcome.HARNESS_ERROR, error=f"setup raised: {exc!r}")
                for case in self.cases
            ]
            return results

        try:
            gate = asyncio.Semaphore(self.config.concurrency)
            stop = asyncio.Event()

            async def one(case: Case) -> CaseRecord:
                if stop.is_set():
                    return self._record(case, Outcome.SKIPPED, error="stopped after failure")
                async with gate:
                    record = await self._execute(case)
                if self.config.on_result:
                    self.config.on_result(record)
                if self.config.fail_fast and record.outcome != Outcome.PASSED.value:
                    stop.set()
                return record

            results.records = list(
                await asyncio.gather(*(one(case) for case in self.cases))
            )
        finally:
            # Teardown must run even when every case failed, and its own
            # failure must not overwrite the results we already have - a
            # scratch directory that would not delete is not a verdict.
            with suppress(Exception):
                await self.target.teardown()

        manifest.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        manifest.target_version = self.target.version
        return results

    # -- one case ----------------------------------------------------------

    async def _execute(self, case: Case) -> CaseRecord:
        if case.skip:
            return self._record(case, Outcome.SKIPPED, error=case.skip)

        seed = case.seed_for(self.cases.seed)
        attempts = 0
        last_error: str | None = None
        started = time.monotonic()

        while attempts < self.config.max_infra_retries + 1:
            attempts += 1
            outcome, observation, error = await self._attempt(case, seed)

            if not outcome.retryable:
                duration = int((time.monotonic() - started) * 1000)
                if observation is None:
                    return self._record(
                        case, outcome, error=error, attempts=attempts,
                        seed=seed, duration_ms=duration,
                    )
                return await self._grade(
                    case, observation, attempts=attempts, seed=seed, duration_ms=duration
                )

            last_error = error
            if attempts <= self.config.max_infra_retries:
                await asyncio.sleep(self.config.retry_backoff_s * attempts)

        return self._record(
            case, Outcome.TARGET_UNAVAILABLE, error=last_error, attempts=attempts,
            seed=seed, duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def _attempt(
        self, case: Case, seed: int
    ) -> tuple[Outcome, Observation | None, str | None]:
        """Execute once and classify the result. No grading happens here."""
        # Deliberately `Any`: `Target` is a Protocol, so its declared return
        # type is a promise the adapter makes, not one the runtime enforces.
        # Trusting it here would turn a third-party adapter's bug into a
        # confusing failure attributed to the target.
        observation: Any
        try:
            observation = await asyncio.wait_for(
                self.target.execute(case, seed=seed), timeout=case.timeout_s
            )
        except TimeoutError:
            return Outcome.TIMEOUT, None, f"exceeded {case.timeout_s}s"
        except TargetUnavailable as exc:
            return Outcome.TARGET_UNAVAILABLE, None, str(exc)
        except (ConnectionError, OSError) as exc:
            return Outcome.INFRA_ERROR, None, f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            # The adapter raised something it did not classify. That is a bug
            # in the harness or the adapter, not a verdict on the target.
            return Outcome.HARNESS_ERROR, None, f"{type(exc).__name__}: {exc}"

        if not isinstance(observation, Observation):
            return (
                Outcome.HARNESS_ERROR, None,
                f"adapter returned {type(observation).__name__}, expected Observation",
            )
        return Outcome.PASSED, observation, None

    async def _grade(
        self, case: Case, observation: Observation, *, attempts: int, seed: int, duration_ms: int
    ) -> CaseRecord:
        try:
            grades = await grade_all(
                case, observation, judge_provider=self.config.judge_provider
            )
        except Exception as exc:
            return self._record(
                case, Outcome.HARNESS_ERROR, observation=observation,
                error=f"grader raised: {type(exc).__name__}: {exc}",
                attempts=attempts, seed=seed, duration_ms=duration_ms,
            )

        # A check that could not run is not a pass. `applicable=False` fails
        # the case loudly rather than quietly counting as green.
        failed = [g for g in grades if not g.passed]
        outcome = Outcome.PASSED if not failed else Outcome.ASSERTION_FAILED
        return self._record(
            case, outcome, observation=observation, grades=grades,
            attempts=attempts, seed=seed, duration_ms=duration_ms,
            error="; ".join(f"{g.kind}: {g.reason}" for g in failed[:3]) or None,
        )

    def _record(
        self,
        case: Case,
        outcome: Outcome,
        *,
        observation: Observation | None = None,
        grades: list[Any] | None = None,
        error: str | None = None,
        attempts: int = 1,
        seed: int = 0,
        duration_ms: int = 0,
    ) -> CaseRecord:
        return CaseRecord.build(
            case=case,
            outcome=outcome,
            case_set_version=self.cases.version,
            target_name=self.target.name,
            target_version=getattr(self.target, "version", "unknown"),
            observation=observation,
            grades=grades,
            duration_ms=duration_ms,
            attempts=attempts,
            seed=seed,
            error=error,
        )
