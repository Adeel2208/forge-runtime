"""Identifier and content-hash helpers.

Idempotency keys are the mechanism that makes crash-resume safe: an action
that has already produced an observed effect must never produce a second one.
The key is derived purely from content, so a resumed run recomputes exactly
the same key for the same intended action.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

__all__ = ["canonical_json", "content_hash", "idempotency_key", "new_id"]


def new_id(prefix: str) -> str:
    """A sortable-enough, readable identifier: ``run_9f2c1a4b``."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace.

    Used for hashing, so it must be stable across processes and Python runs.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(*parts: Any) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(canonical_json(part).encode("utf-8"))
        digest.update(b"\x1f")  # unit separator: prevents boundary collisions
    return digest.hexdigest()


def idempotency_key(run_id: str, tool: str, arguments: dict[str, Any], attempt_group: int) -> str:
    """Stable key for one *intended* external effect.

    ``attempt_group`` is bumped only when the runtime deliberately wants a
    fresh effect (e.g. after a compensation), never on a plain retry - a retry
    must reuse the key so the effect is not duplicated.
    """
    return content_hash(run_id, tool, arguments, attempt_group)[:32]
