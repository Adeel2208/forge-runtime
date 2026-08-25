"""Loop detection (spec §7).

Three independent signals, because agents loop in three different ways:

* the same action repeated verbatim (a stuck tool call),
* a short cycle of actions repeating (A, B, A, B, ...),
* no forward progress - steps burning with no new observations.

Any one tripping halts the run. This is a bound, not a heuristic: it fires on
counting, so it cannot be argued out of by a persuasive model.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["LoopDetector", "LoopSignal"]


@dataclass(frozen=True)
class LoopSignal:
    tripped: bool
    kind: str = ""
    detail: str = ""

    def __bool__(self) -> bool:
        return self.tripped


NO_LOOP = LoopSignal(tripped=False)


@dataclass
class LoopDetector:
    max_identical: int = 3
    """Same fingerprint this many times in a row -> stuck."""

    max_cycle_repeats: int = 3
    """A cycle of length 2..4 repeating this many times -> oscillating."""

    max_steps_without_progress: int = 5

    fingerprints: list[str] = field(default_factory=list)
    _last_observation_count: int = 0
    _stagnant_steps: int = 0

    def record_action(self, fingerprint: str) -> LoopSignal:
        self.fingerprints.append(fingerprint)

        tail = self.fingerprints[-self.max_identical :]
        if len(tail) == self.max_identical and len(set(tail)) == 1:
            return LoopSignal(
                tripped=True,
                kind="identical_action",
                detail=f"{fingerprint} repeated {self.max_identical}x",
            )

        for size in (2, 3, 4):
            window = size * self.max_cycle_repeats
            if len(self.fingerprints) < window:
                continue
            recent = self.fingerprints[-window:]
            cycle = recent[:size]
            if all(recent[i : i + size] == cycle for i in range(0, window, size)):
                return LoopSignal(
                    tripped=True,
                    kind="cyclic_actions",
                    detail=f"cycle of {size} repeated {self.max_cycle_repeats}x",
                )

        return NO_LOOP

    def record_step(self, observation_count: int) -> LoopSignal:
        """Called once per committed step to track forward progress."""
        if observation_count > self._last_observation_count:
            self._last_observation_count = observation_count
            self._stagnant_steps = 0
            return NO_LOOP

        self._stagnant_steps += 1
        if self._stagnant_steps >= self.max_steps_without_progress:
            return LoopSignal(
                tripped=True,
                kind="no_progress",
                detail=f"{self._stagnant_steps} steps produced no new observation",
            )
        return NO_LOOP

    def reset(self) -> None:
        self.fingerprints.clear()
        self._last_observation_count = 0
        self._stagnant_steps = 0
