"""Time, injectable.

Deterministic replay (spec §19) is impossible if the runtime calls
``datetime.now()`` directly, so every timestamp goes through a Clock that
tests and the replay engine can substitute.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

__all__ = ["Clock", "FrozenClock", "SystemClock"]


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic_ms(self) -> int: ...


class SystemClock:
    """Wall-clock time. The default everywhere outside tests and replay."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ms(self) -> int:
        import time

        return int(time.monotonic() * 1000)


class FrozenClock:
    """A clock that only advances when told to.

    Makes latency assertions exact and keeps replayed trajectories
    byte-identical to the originals.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._mono = 0

    def now(self) -> datetime:
        return self._now

    def monotonic_ms(self) -> int:
        return self._mono

    def advance(self, ms: int) -> None:
        from datetime import timedelta

        self._mono += ms
        self._now = self._now + timedelta(milliseconds=ms)
