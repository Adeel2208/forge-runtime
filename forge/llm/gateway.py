"""The LLM gateway: routing, fallback, retries and cost accounting (spec §15).

Three properties the rest of the runtime relies on:

1. Application code never learns which provider served a call. Selection is a
   *routing decision*, recorded like any other decision the runtime makes.
2. Spend is checked before a request leaves the process, not tallied after it
   returns. A call whose projected cost would breach the run's ceiling is not
   made; the gateway moves down the fallback chain instead.
3. A provider outage degrades into a logged failover rather than a run
   failure. Every attempt - taken, skipped or failed - is reported through
   `on_attempt`, so the whole routing chain lands in the trace.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from forge.core.contracts import Usage
from forge.errors import BudgetExhausted, ProviderUnavailable
from forge.llm.base import LLMProvider, ModelRequest, ModelResponse

__all__ = ["CostLedger", "LLMGateway", "RouteAttempt"]


@dataclass
class CostLedger:
    """Running spend and token totals for one run, checked before every call.

    The ledger is authoritative for *routing*: the gateway consults it to
    decide whether a provider is affordable, which is why the ceiling lives
    here rather than being inspected after the fact by a dashboard.
    """

    usd_ceiling: float = 5.0
    token_ceiling: int = 250_000
    spent_usd: float = 0.0
    spent_tokens: int = 0
    calls: int = 0

    def would_exceed(self, projected_usd: float) -> bool:
        return round(self.spent_usd + projected_usd, 10) > self.usd_ceiling

    @property
    def remaining_usd(self) -> float:
        return round(max(0.0, self.usd_ceiling - self.spent_usd), 10)

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
            # -- the spend gate, applied before the request is sent.
            #    Projected on the request's own token ceiling, so the estimate
            #    is an upper bound rather than a hopeful guess.
            projected = provider.pricing.cost(
                Usage(input_tokens=request.max_tokens, output_tokens=request.max_tokens)
            )
            if self.ledger.would_exceed(projected):
                attempts.append(
                    RouteAttempt(
                        provider=provider.name,
                        model=provider.model,
                        ok=False,
                        reason=(
                            f"skipped: projected ${projected:.4f} would exceed the "
                            f"remaining budget of ${self.ledger.remaining_usd:.4f}"
                        ),
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
            raise BudgetExhausted(
                "no provider fits the remaining budget",
                reason=(
                    f"${self.ledger.remaining_usd:.4f} left of a "
                    f"${self.ledger.usd_ceiling:.2f} ceiling"
                ),
                policy_version="cost-ledger",
            )
        raise ProviderUnavailable(
            "all providers failed",
            attempts=[a.reason for a in attempts],
        ) from last_error

    def _notify(self, attempt: RouteAttempt) -> None:
        if self.on_attempt is not None:
            self.on_attempt(attempt)
