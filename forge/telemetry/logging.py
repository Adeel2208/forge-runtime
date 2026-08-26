"""Structured logging.

Operations reads logs; humans read logs at 3am. Both are served by one JSON
object per line with stable field names, and neither is served by f-strings
interpolated into prose.

Dependency-free on purpose - it wraps `logging`, so it composes with whatever
the host application already configured, and adds no third-party logger to a
library other people will embed.

Every value passes through the same redaction used for telemetry, so a secret
cannot reach a log line by being passed as a keyword.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from forge.telemetry.tracer import redact

__all__ = ["BoundLogger", "JsonFormatter", "configure_logging", "get_logger"]


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "forge_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class BoundLogger:
    """A logger that takes keyword fields instead of formatted strings.

        log.info("recovered run", run_id=run_id, duplicate_effects=0)
    """

    __slots__ = ("_bound", "_logger")

    def __init__(self, logger: logging.Logger, bound: dict[str, Any] | None = None) -> None:
        self._logger = logger
        self._bound = bound or {}

    def bind(self, **fields: Any) -> BoundLogger:
        """Attach fields to every subsequent line - a run id, a tenant."""
        return BoundLogger(self._logger, {**self._bound, **fields})

    def _emit(self, level: int, message: str, fields: dict[str, Any]) -> None:
        if not self._logger.isEnabledFor(level):
            return
        merged = redact({**self._bound, **fields})
        self._logger.log(level, message, extra={"forge_fields": merged})

    def debug(self, message: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, message, fields)

    def info(self, message: str, **fields: Any) -> None:
        self._emit(logging.INFO, message, fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._emit(logging.WARNING, message, fields)

    def error(self, message: str, **fields: Any) -> None:
        self._emit(logging.ERROR, message, fields)

    def exception(self, message: str, **fields: Any) -> None:
        merged = redact({**self._bound, **fields})
        self._logger.exception(message, extra={"forge_fields": merged})


def get_logger(name: str = "forge") -> BoundLogger:
    return BoundLogger(logging.getLogger(name))


def configure_logging(
    *, level: str = "INFO", json_output: bool = True, stream: Any = None
) -> None:
    """Install a handler on the `forge` logger.

    A library must not configure the root logger, so this touches only
    `forge.*` and stops propagation. An embedding application keeps control of
    everything else.
    """
    logger = logging.getLogger("forge")
    logger.handlers.clear()
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s  %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False
