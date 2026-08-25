"""Failure taxonomy.

Every exception carries a `retry_class`, because the recovery policy (spec §9)
is driven by *why* something failed, not by where it was raised. An error whose
class is unknown is treated as UNRECOVERABLE - failing closed, not open.
"""

from __future__ import annotations

from forge.core.enums import RetryClass

__all__ = [
    "BudgetExhausted",
    "DeterministicError",
    "EffectMismatch",
    "ForgeError",
    "InvalidTransition",
    "LoopDetected",
    "PolicyDenied",
    "ProposalInvalid",
    "ProviderUnavailable",
    "TransientError",
    "UnrecoverableError",
]


class ForgeError(Exception):
    """Base class. Fails closed: unknown errors are not retried."""

    retry_class: RetryClass = RetryClass.UNRECOVERABLE

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def __str__(self) -> str:
        if not self.context:
            return self.message
        detail = " ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({detail})"


class TransientError(ForgeError):
    """Worth retrying unchanged: timeouts, 429s, connection resets."""

    retry_class = RetryClass.TRANSIENT


class ProviderUnavailable(TransientError):
    """A model provider could not be reached or refused the request."""


class DeterministicError(ForgeError):
    """Retrying identically will fail identically; the input must change."""

    retry_class = RetryClass.DETERMINISTIC


class ProposalInvalid(DeterministicError):
    """The model's proposal failed schema or invariant validation."""


class PolicyDenied(ForgeError):
    """The trust plane refused the action. Never retried as-is."""

    retry_class = RetryClass.POLICY_BLOCKED

    def __init__(self, message: str, *, reason: str = "", policy_version: str = "", **ctx: object):
        super().__init__(message, reason=reason, policy_version=policy_version, **ctx)
        self.reason = reason
        self.policy_version = policy_version


class BudgetExhausted(PolicyDenied):
    """A resource ceiling was reached: tokens, wall-clock, steps or USD."""


class UnrecoverableError(ForgeError):
    """The run cannot continue and must be failed."""


class InvalidTransition(UnrecoverableError):
    """An illegal lifecycle transition was attempted - a runtime bug."""


class EffectMismatch(ForgeError):
    """Observed effect did not match the authorized intent (spec §5 RECONCILE)."""

    retry_class = RetryClass.DETERMINISTIC


class LoopDetected(ForgeError):
    """Repeated state or action exceeded its bound."""

    retry_class = RetryClass.UNRECOVERABLE
