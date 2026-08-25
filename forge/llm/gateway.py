"""The LLM gateway: routing, fallback, retries and cost accounting (spec §15).

Two properties the rest of the runtime relies on:

1. Application code never knows which provider served a call. Tier selection
   is a *policy decision*, logged like any other.
2. A provider that costs money cannot be reached without the `PAID_INFERENCE`
   capability. A free-tier 429 therefore degrades into a logged fallback
   rather than an outage - which is exactly the resilience behaviour §13 asks
   us to demonstrate, obtained for free from the budget constraint.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from forge.core.contracts import Usage
from forge.errors import BudgetExhausted, PolicyDenied, ProviderUnavailable
from forge.llm.base import LLMProvider, ModelRequest, ModelResponse

__all__ = ["CostLedger", "LLMGateway", "RouteAttempt"]


@dataclass
class CostLedger:
    """Running cost and token totals, checked before every call.

    `usd_ceiling` defaults to 0.0: the runtime refuses to spend money unless
    someone explicitly raises it. That is the whole trick.
    """

    usd_ceiling: float = 0.0
    token_ceiling: int = 250_000
    spent_usd: float = 0.0
    spent_tokens: int = 0
    calls: int = 0

    def would_exceed(self, projected_usd: float) -> bool:
        return round(self.spent_usd + projected_usd, 10) > self.usd_ceiling

    def check_headroom(self) -> None:
        if self.spent_tokens >= self.token_ceiling:
            raise BudgetExhausted(
                "token ceiling reached",
                reason=f"{self.spent_tokens} >= {self.token_ceiling}",
            )

    def record(self, usage: Usage) -> None:
        self.spent_usd = round(self.spent_usd + usage.usd, 10)
        self.spent_tokens += usage.total_tokens
        self.calls += 1

    def snapshot(self) -> dict[str, float | int]:
        return {
            "usd": self.spent_usd,
            "usd_ceiling": self.usd_ceiling,
            "tokens": self.spent_tokens,
            "token_ceiling": self.token_ceiling,
            "calls": self.calls,
        }


@dataclass
class RouteAttempt:
    """One provider attempt, kept so the caller can log the whole chain."""

    provider: str
    model: str
    ok: bool
    reason: str = ""
    latency_ms: int = 0


@dataclass
class LLMGateway:
    """Ordered provider chain with policy-gated, cost-aware fallback."""

    providers: Sequence[LLMProvider]
    ledger: CostLedger = field(default_factory=CostLedger)
    max_attempts_per_provider: int = 2
    on_attempt: Callable[[RouteAttempt], None] | None = None
    """Hook so the runtime can emit a telemetry event per attempt."""

    def __post_init__(self) -> None:
        if not self.providers:
            raise ValueError("LLMGateway needs at least one provider")

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Try each provider in order; return the first success.

        Raises `PolicyDenied` if every provider was blocked by budget, and
        `ProviderUnavailable` if every provider was tried and failed. The
        distinction matters: one is a governance outcome, the other an outage.
        """
        self.ledger.check_headroom()

        attempts: list[RouteAttempt] = []
        blocked_all = True
        last_error: Exception | None = None

        for provider in self.providers:
            # -- the cost gate. A paid provider under a $0 ceiling never runs.
            projected = provider.pricing.cost(
                Usage(input_tokens=request.max_tokens, output_tokens=request.max_tokens)
            )
            if not provider.pricing.is_free and self.ledger.would_exceed(projected):
                attempts.append(
                    RouteAttempt(
                        provider=provider.name,
                        model=provider.model,
                        ok=False,
                        reason=f"blocked: would exceed usd_ceiling={self.ledger.usd_ceiling}",
                    )
                )
                self._notify(attempts[-1])
                continue

            blocked_all = False
            for attempt in range(1, self.max_attempts_per_provider + 1):
                try:
                    response = await provider.complete(request)
                except ProviderUnavailable as exc:
                    last_error = exc
                    attempts.append(
                        RouteAttempt(
                            provider=provider.name,
                            model=provider.model,
                            ok=False,
                            reason=f"attempt {attempt}: {exc.message}",
                        )
                    )
                    self._notify(attempts[-1])
                    continue

                priced = response.model_copy(
                    update={
                        "usage": response.usage.model_copy(
                            update={"usd": provider.pricing.cost(response.usage)}
                        )
                    }
                )
                self.ledger.record(priced.usage)
                attempts.append(
                    RouteAttempt(
                        provider=provider.name,
                        model=provider.model,
                        ok=True,
                        latency_ms=priced.latency_ms,
                    )
                )
                self._notify(attempts[-1])
                return priced

        if blocked_all:
            raise PolicyDenied(
                "every provider was blocked by the cost ceiling",
                reason="no free-tier provider available",
                policy_version="cost-ledger",
            )
        raise ProviderUnavailable(
            "all providers failed",
            attempts=[a.reason for a in attempts],
        ) from last_error

    def _notify(self, attempt: RouteAttempt) -> None:
        if self.on_attempt is not None:
            self.on_attempt(attempt)
