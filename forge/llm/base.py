"""Provider-neutral model interface (spec §15).

`Pricing` is not decoration. The gateway projects a call's cost from it and
checks that against the run's remaining budget *before* the request is sent,
so a run cannot discover it is over budget by having already gone over
(docs/adr/0003-spend-and-capability-governance.md).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from forge.core.contracts import Usage

__all__ = ["LLMProvider", "ModelRequest", "ModelResponse", "Pricing"]


class Pricing(BaseModel):
    """USD per 1k tokens. Zero means free at the tier we are entitled to use."""

    model_config = ConfigDict(frozen=True)

    input_per_1k: float = 0.0
    output_per_1k: float = 0.0

    @property
    def is_free(self) -> bool:
        return self.input_per_1k == 0.0 and self.output_per_1k == 0.0

    def cost(self, usage: Usage) -> float:
        return round(
            usage.input_tokens / 1000 * self.input_per_1k
            + usage.output_tokens / 1000 * self.output_per_1k,
            10,
        )


class ModelRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    system: str
    messages: list[dict[str, str]]
    response_schema: dict[str, Any] | None = None
    """When set, the provider must return JSON conforming to this schema."""

    tools: list[dict[str, Any]] = Field(default_factory=list)
    temperature: float = 0.0
    max_tokens: int = 1024
    timeout_s: float = 60.0

    def digest_source(self) -> dict[str, Any]:
        """The parts that must match for a replayed call to count as identical."""
        return {
            "system": self.system,
            "messages": self.messages,
            "response_schema": self.response_schema,
            "temperature": self.temperature,
        }


class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    parsed: dict[str, Any] | None = None
    usage: Usage = Field(default_factory=Usage)
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    finish_reason: str = "stop"
    raw: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """What every adapter must offer. Note how little it is."""

    name: str
    model: str
    pricing: Pricing

    async def complete(self, request: ModelRequest) -> ModelResponse: ...

    async def healthy(self) -> bool: ...
