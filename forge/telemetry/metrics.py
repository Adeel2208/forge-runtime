"""Counters and histograms, Prometheus-shaped (spec §16).

Kept dependency-free so metrics are assertable in unit tests. `render()` emits
the Prometheus text exposition format, which the optional FastAPI app serves
at `/metrics` without any client library.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

__all__ = ["Metrics"]

Labels = tuple[tuple[str, str], ...]


def _labels(pairs: dict[str, str] | None) -> Labels:
    return tuple(sorted((pairs or {}).items()))


@dataclass
class Metrics:
    counters: dict[tuple[str, Labels], float] = field(default_factory=lambda: defaultdict(float))
    observations: dict[tuple[str, Labels], list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        self.counters[(name, _labels(labels))] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        self.observations[(name, _labels(labels))].append(value)

    def get(self, name: str, **labels: str) -> float:
        return self.counters.get((name, _labels(labels)), 0.0)

    def summary(self, name: str, **labels: str) -> dict[str, float]:
        values = sorted(self.observations.get((name, _labels(labels)), []))
        if not values:
            return {"count": 0, "p50": 0.0, "p95": 0.0, "max": 0.0}
        return {
            "count": len(values),
            "p50": values[len(values) // 2],
            "p95": values[min(len(values) - 1, int(len(values) * 0.95))],
            "max": values[-1],
        }

    def render(self) -> str:
        """Prometheus text exposition format."""
        lines: list[str] = []
        for (name, labels), value in sorted(self.counters.items()):
            lines.append(f"{name}{_fmt(labels)} {value}")
        for (name, labels), values in sorted(self.observations.items()):
            if not values:
                continue
            stats = self.summary(name, **dict(labels))
            lines.append(f"{name}_count{_fmt(labels)} {stats['count']}")
            lines.append(f"{name}_sum{_fmt(labels)} {sum(values)}")
            for quantile in ("p50", "p95"):
                q = "0.5" if quantile == "p50" else "0.95"
                lines.append(f"{name}{_fmt(labels, quantile=q)} {stats[quantile]}")
        return "\n".join(lines) + "\n"


def _fmt(labels: Labels, **extra: str) -> str:
    pairs = list(labels) + sorted(extra.items())
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in pairs)
    return "{" + inner + "}"
