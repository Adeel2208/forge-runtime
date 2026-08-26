"""Failure classes.

Conflating these destroys the signal, so they are a closed vocabulary rather
than a boolean and a log line. The distinction that matters most in practice:

    ASSERTION_FAILED   the target ran correctly and produced a wrong answer
    TARGET_UNAVAILABLE the target could not be reached at all
    INFRA_ERROR        the harness's own plumbing flaked
    HARNESS_ERROR      the harness has a bug

The first is a fact about the system under test. The other three are facts
about the environment, and reporting them as test failures is how a suite
stops being believed.

**Retries are for infrastructure only.** `Outcome.retryable` encodes this: a
failed assertion is never re-rolled, because re-rolling an assertion until it
passes is not testing, it is sampling until you like the answer.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["RETRYABLE", "TERMINAL_FAILURES", "Outcome"]


class Outcome(StrEnum):
    PASSED = "PASSED"
    ASSERTION_FAILED = "ASSERTION_FAILED"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
    INFRA_ERROR = "INFRA_ERROR"
    HARNESS_ERROR = "HARNESS_ERROR"
    SKIPPED = "SKIPPED"
    TIMEOUT = "TIMEOUT"

    @property
    def retryable(self) -> bool:
        """Only environmental problems may be retried."""
        return self in RETRYABLE

    @property
    def is_target_verdict(self) -> bool:
        """True when this outcome says something about the system under test.

        Trend lines, regression gates and pass rates must be computed over
        these only - mixing in infra noise makes a flaky network look like a
        quality regression.
        """
        return self in (Outcome.PASSED, Outcome.ASSERTION_FAILED, Outcome.TIMEOUT)

    @property
    def passed(self) -> bool:
        return self is Outcome.PASSED


RETRYABLE: frozenset[Outcome] = frozenset(
    {Outcome.TARGET_UNAVAILABLE, Outcome.INFRA_ERROR}
)

TERMINAL_FAILURES: frozenset[Outcome] = frozenset(
    {Outcome.ASSERTION_FAILED, Outcome.HARNESS_ERROR, Outcome.TIMEOUT}
)
