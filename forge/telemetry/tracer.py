"""Tracing (spec §16).

OpenTelemetry-*compatible* rather than OpenTelemetry-*dependent*: spans are
always recorded in-process (so tests and the benchmark runner can assert on
them with no collector running), and are additionally mirrored to a real OTel
tracer when the SDK is installed and a provider is configured.

Redaction happens before persistence, per §19: we store what the agent *did*,
never its private reasoning.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from forge.ids import new_id

__all__ = ["REDACTED", "Span", "Tracer", "redact"]

REDACTED = "[redacted]"

_SENSITIVE_KEYS = frozenset(
    {
        "api_key", "apikey", "authorization", "token", "password", "secret",
        "reasoning", "chain_of_thought", "thinking", "raw_completion",
    }
)


def redact(value: Any, *, depth: int = 0) -> Any:
    """Strip sensitive keys and truncate long strings before persistence."""
    if depth > 6:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if str(k).lower() in _SENSITIVE_KEYS else redact(v, depth=depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, depth=depth + 1) for v in value[:50]]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + f"...[+{len(value) - 2000} chars]"
    return value


@dataclass
class Span:
    name: str
    span_id: str = field(default_factory=lambda: new_id("span"))
    parent_id: str | None = None
    trace_id: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    started_ms: float = 0.0
    duration_ms: int = 0
    status: str = "OK"
    error: str | None = None

    def set(self, **attrs: Any) -> None:
        self.attributes.update(redact(attrs))


class Tracer:
    """Records spans in memory; mirrors to OpenTelemetry when available."""

    def __init__(self, *, trace_id: str | None = None, otel: bool = False) -> None:
        self.trace_id = trace_id or new_id("trace")
        self.spans: list[Span] = []
        self._stack: list[Span] = []
        self._otel = _load_otel_tracer() if otel else None

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        span = Span(
            name=name,
            parent_id=self._stack[-1].span_id if self._stack else None,
            trace_id=self.trace_id,
            attributes=redact(attributes),
            started_ms=time.monotonic() * 1000,
        )
        self._stack.append(span)
        otel_cm = self._otel.start_as_current_span(name) if self._otel else None
        otel_span = otel_cm.__enter__() if otel_cm else None
        try:
            yield span
        except Exception as exc:
            span.status = "ERROR"
            span.error = f"{type(exc).__name__}: {exc}"
            if otel_span is not None:
                otel_span.record_exception(exc)
            raise
        finally:
            span.duration_ms = int(time.monotonic() * 1000 - span.started_ms)
            if otel_span is not None:
                for key, value in span.attributes.items():
                    otel_span.set_attribute(f"forge.{key}", str(value))
            if otel_cm is not None:
                otel_cm.__exit__(None, None, None)
            self._stack.pop()
            self.spans.append(span)

    # -- introspection -----------------------------------------------------

    def by_name(self, name: str) -> list[Span]:
        return [s for s in self.spans if s.name == name]

    def to_list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": s.name,
                "span_id": s.span_id,
                "parent_id": s.parent_id,
                "trace_id": s.trace_id,
                "duration_ms": s.duration_ms,
                "status": s.status,
                "error": s.error,
                "attributes": s.attributes,
            }
            for s in self.spans
        ]


def _load_otel_tracer() -> Any:
    """Return a real OTel tracer, or None if the SDK is absent."""
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    return trace.get_tracer("forge.runtime")
