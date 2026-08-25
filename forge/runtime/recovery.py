"""Retry classification and backoff (spec §9).

The classification is the interesting part, not the backoff. A transient
failure is retried unchanged with the *same* idempotency key - which is why a
retried write does not become two writes. A deterministic failure is never
retried unchanged, because it would fail identically; the input must change,
so it goes back through VIEW as a repair.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from forge.core.enums import RetryClass
from forge.errors import ForgeError

__all__ = ["RetryPolicy", "backoff_ms", "classify"]

_DEFAULT_RNG = random.Random()
"""Used only when no seeded generator is supplied. Benchmarks always supply one."""


def classify(exc: BaseException) -> RetryClass:
    """Map an exception to its recovery class. Unknown failures fail closed."""
    if isinstance(exc, ForgeError):
        return exc.retry_class
    if isinstance(exc, TimeoutError):
        return RetryClass.TRANSIENT
    if isinstance(exc, ConnectionError | OSError):
        return RetryClass.TRANSIENT
    if isinstance(exc, ValueError | TypeError | KeyError):
        return RetryClass.DETERMINISTIC
    return RetryClass.UNRECOVERABLE


def backoff_ms(attempt: int, *, base_ms: int = 50, cap_ms: int = 5_000, jitter: bool = True,
               rng: random.Random | None = None) -> int:
    """Exponential backoff with full jitter.

    Jitter is seedable so benchmark runs stay reproducible: an unseeded
    `random` would make added-latency figures non-comparable between runs.
    """
    # Shift rather than `**`: it is unambiguously integral, and clamping the
    # exponent stops a large `attempt` computing an enormous number that the
    # cap would only throw away.
    exponent = min(max(0, attempt - 1), 32)
    raw = min(cap_ms, base_ms * (1 << exponent))
    if not jitter:
        return raw
    generator: random.Random = rng if rng is not None else _DEFAULT_RNG
    return int(generator.uniform(0, raw))


@dataclass
class RetryPolicy:
    """Per-class attempt ceilings."""

    max_transient: int = 3
    max_deterministic: int = 2
    base_ms: int = 50
    cap_ms: int = 5_000

    def should_retry(self, retry_class: RetryClass, attempts_so_far: int) -> bool:
        match retry_class:
            case RetryClass.TRANSIENT:
                return attempts_so_far < self.max_transient
            case RetryClass.DETERMINISTIC:
                # Retried only via a repair path that changes the input.
                return attempts_so_far < self.max_deterministic
            case RetryClass.POLICY_BLOCKED | RetryClass.UNRECOVERABLE:
                return False

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> int:
        return backoff_ms(attempt, base_ms=self.base_ms, cap_ms=self.cap_ms, rng=rng)
