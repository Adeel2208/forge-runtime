"""The append-only event envelope.

Every meaningful thing that happens becomes one of these. Canonical state is
defined as the fold of the event stream (`forge.state.projection`), which is
what makes crash-resume and replay the *same* mechanism rather than two
parallel implementations that can drift apart.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from forge.core.enums import EventType
from forge.ids import content_hash

__all__ = ["Event", "NewEvent"]


class NewEvent(BaseModel):
    """An event awaiting a sequence number - what callers construct."""

    model_config = ConfigDict(extra="forbid")

    type: EventType
    run_id: str
    step_id: str | None = None
    step_index: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    """Set on effect events. The store enforces uniqueness per run."""

    def digest(self) -> str:
        return content_hash(self.type, self.run_id, self.step_index, self.payload)


class Event(NewEvent):
    """A persisted event. `seq` is the global, monotonic ordering."""

    seq: int
    ts: datetime

    def summary(self) -> str:
        """One-line, redaction-safe rendering for CLI trace output."""
        bits = [f"#{self.seq:>4}", self.type.value]
        if self.step_index is not None:
            bits.insert(1, f"step={self.step_index}")
        for key in ("phase", "tool", "decision", "reason", "provider", "fault", "ok"):
            if key in self.payload:
                bits.append(f"{key}={self.payload[key]}")
        return " ".join(str(b) for b in bits)
