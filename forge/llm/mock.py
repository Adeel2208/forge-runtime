"""Deterministic provider (spec §15, §19).

This is written first, on purpose. Making the *first* provider not a provider
at all forces the gateway abstraction to be honest: there is no opportunity to
leak an HTTP client, a vendor field name, or a network assumption into the
runtime, because there is nothing to leak it from.

It is also what makes the test suite hermetic: every unit, integration,
recovery, adversarial and evaluation run executes against a fixed script, so a
failure is a fact about the code rather than about today's model weights.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from forge.core.contracts import Usage
from forge.errors import ProviderUnavailable
from forge.llm.base import ModelRequest, ModelResponse, Pricing

__all__ = ["MockProvider", "ScriptedTurn"]


class ScriptedTurn(BaseModel):
    """One planned model reply, optionally corrupted on purpose."""

    model_config = ConfigDict(frozen=True)

    proposal: dict[str, Any] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=lambda: Usage(input_tokens=120, output_tokens=40))
    latency_ms: int = 5

    malformed: bool = False
    """Emit syntactically invalid JSON - exercises the VALIDATE branch."""

    raise_timeout: bool = False
    """Raise `ProviderUnavailable` - exercises transient-retry recovery."""

    repeat: int = 1
    """Serve this turn N times. Used to script action loops for §18."""


class MockProvider:
    """Serves a fixed script. Byte-identical across processes and machines."""

    def __init__(
        self,
        turns: Sequence[ScriptedTurn | dict[str, Any]],
        *,
        name: str = "mock",
        model: str = "mock-1",
        loop_last: bool = True,
    ) -> None:
        expanded: list[ScriptedTurn] = []
        for turn in turns:
            item = turn if isinstance(turn, ScriptedTurn) else ScriptedTurn(**turn)
            expanded.extend([item] * max(1, item.repeat))
        self._turns = expanded
        self._cursor = 0
        self.name = name
        self.model = model
        self.pricing = Pricing()  # free, by construction
        self.loop_last = loop_last
        self.calls: list[ModelRequest] = []

    # -- construction helpers ---------------------------------------------

    @classmethod
    def from_fixture(cls, path: str | Path, **kwargs: Any) -> MockProvider:
        """Load a script from JSON. Fixtures live in `tests/fixtures/`."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        turns = data["turns"] if isinstance(data, dict) else data
        return cls(turns, **kwargs)

    @classmethod
    def answering(cls, answer: str, **kwargs: Any) -> MockProvider:
        """A one-turn script that answers immediately. Handy in tests."""
        return cls([ScriptedTurn(proposal={"kind": "ANSWER", "answer": answer})], **kwargs)

    # -- provider protocol -------------------------------------------------

    async def healthy(self) -> bool:
        return True

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        turn = self._next_turn()

        if turn.raise_timeout:
            raise ProviderUnavailable(
                "scripted provider timeout", provider=self.name, model=self.model
            )

        if turn.malformed:
            # Truncated JSON: the classic structured-output failure (§18).
            text = json.dumps(turn.proposal)[: max(1, len(json.dumps(turn.proposal)) // 2)]
            return ModelResponse(
                text=text,
                parsed=None,
                usage=turn.usage,
                provider=self.name,
                model=self.model,
                latency_ms=turn.latency_ms,
                finish_reason="length",
            )

        text = json.dumps(turn.proposal)
        return ModelResponse(
            text=text,
            parsed=turn.proposal,
            usage=turn.usage,
            provider=self.name,
            model=self.model,
            latency_ms=turn.latency_ms,
        )

    def _next_turn(self) -> ScriptedTurn:
        if not self._turns:
            raise ProviderUnavailable("mock provider has an empty script", provider=self.name)
        if self._cursor < len(self._turns):
            turn = self._turns[self._cursor]
            self._cursor += 1
            return turn
        if self.loop_last:
            # Past the end, keep serving the final turn. Prevents a script that
            # is one turn short from masquerading as a provider outage.
            return self._turns[-1]
        raise ProviderUnavailable("mock provider script exhausted", provider=self.name)

    # -- introspection -----------------------------------------------------

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def reset(self) -> None:
        self._cursor = 0
        self.calls.clear()
