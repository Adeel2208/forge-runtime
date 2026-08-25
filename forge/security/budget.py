"""Resource budgets (spec §13).

Budgets are checked at AUTHORIZE, not merely reported at the end. A run that
would exceed its ceiling is denied before it spends, which is the difference
between a budget and a bill.
"""

from __future__ import annotations

from dataclasses import dataclass

from forge.errors import BudgetExhausted

__all__ = ["Budget"]


@dataclass
class Budget:
    """Per-run resource ceilings.

    Defaults are deliberately conservative: an agent that misbehaves should hit
    a wall quickly and cheaply. Raise them per task, per tenant or per policy
    bundle - but a run always executes against explicit, finite limits.
    """

    max_steps: int = 24
    max_tool_calls: int = 32
    max_tokens: int = 250_000
    max_wall_clock_s: float = 1800.0
    max_usd: float = 5.0
    """Spend ceiling for a single run, checked before each model call."""

    max_consecutive_failures: int = 4

    steps: int = 0
    tool_calls: int = 0
    tokens: int = 0
    elapsed_s: float = 0.0
    usd: float = 0.0
    consecutive_failures: int = 0

    def check(self) -> None:
        """Raise `BudgetExhausted` if any ceiling has been reached."""
        for label, spent, ceiling in (
            ("steps", self.steps, self.max_steps),
            ("tool_calls", self.tool_calls, self.max_tool_calls),
            ("tokens", self.tokens, self.max_tokens),
            ("wall_clock_s", self.elapsed_s, self.max_wall_clock_s),
            ("usd", self.usd, self.max_usd),
            ("consecutive_failures", self.consecutive_failures, self.max_consecutive_failures),
        ):
            if spent > ceiling:
                raise BudgetExhausted(
                    f"budget exhausted: {label}",
                    reason=f"{label}={spent} exceeds ceiling {ceiling}",
                    policy_version="budget",
                )

    def remaining(self) -> dict[str, float]:
        return {
            "steps": self.max_steps - self.steps,
            "tool_calls": self.max_tool_calls - self.tool_calls,
            "tokens": self.max_tokens - self.tokens,
            "usd": round(self.max_usd - self.usd, 10),
        }

    def note_success(self) -> None:
        self.consecutive_failures = 0

    def note_failure(self) -> None:
        self.consecutive_failures += 1

    def snapshot(self) -> dict[str, float | int]:
        return {
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "usd": self.usd,
            "elapsed_s": round(self.elapsed_s, 3),
        }
