"""Content-hash anchoring: pinning a note to a region of code.

A repository fact is true about a *piece of code*, not about a *point in time*.
"This function is not thread-safe" does not become less true because it is six
months old, and it becomes false the instant someone adds a lock - which no
time-decay heuristic can detect. So anchors hash the region, and staleness is
an exact comparison rather than an age threshold.

The tree snapshot is a plain mapping of path to file content. That keeps this
module pure and testable with dict fixtures, and keeps filesystem access in the
caller where it belongs.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from forge.knowledge.models import Anchor, Region

__all__ = [
    "TreeSnapshot",
    "anchor_for",
    "hash_region",
    "is_stale",
    "resolve_region",
    "stale_anchors",
]

TreeSnapshot = Mapping[str, str]

#: Definition forms we can locate by name. Deliberately shallow: this is a
#: lexical heuristic for C-family and Python sources, not a parser. When it
#: cannot find the symbol the anchor reads as stale, which is the safe
#: direction - a note that may no longer apply should stop being canonical.
_DEF_PATTERNS = (
    r"^\s*(?:async\s+)?def\s+{name}\s*[\(\[]",
    r"^\s*class\s+{name}\s*[\(\:\[]",
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+{name}\s*[\(\<]",
    r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+{name}\s*[\(\<]",
    r"^\s*(?:type|interface|struct|enum)\s+{name}\b",
    r"^\s*(?:const|let|var)\s+{name}\s*=",
)


def _normalise(text: str) -> str:
    """Line endings only.

    Whitespace *within* a line is content: reindenting a block genuinely may
    change its meaning, and in Python it certainly does. Only `\\r\\n` is
    normalised, so a note written on Windows and checked on Linux does not
    read as stale for no reason. This repository has been bitten by
    platform asymmetry before.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def hash_region(content: str, region: Region) -> str | None:
    """Hash the addressed region, or None if it cannot be located."""
    span = resolve_region(content, region)
    if span is None:
        return None
    start, end = span
    lines = _normalise(content).split("\n")
    body = "\n".join(lines[start - 1 : end])
    digest = hashlib.sha256()
    digest.update(body.encode("utf-8"))
    return digest.hexdigest()


def resolve_region(content: str, region: Region) -> tuple[int, int] | None:
    """Turn a region into a concrete 1-based inclusive line span."""
    lines = _normalise(content).split("\n")
    if isinstance(region, str):
        return _symbol_span(lines, region)
    start, end = region
    if start < 1 or end < start or start > len(lines):
        return None
    return (start, min(end, len(lines)))


def _symbol_span(lines: list[str], symbol: str) -> tuple[int, int] | None:
    """The definition line of `symbol` through the end of its indented block."""
    escaped = re.escape(symbol)
    patterns = [re.compile(p.format(name=escaped)) for p in _DEF_PATTERNS]

    start_idx: int | None = None
    for idx, line in enumerate(lines):
        if any(p.match(line) for p in patterns):
            start_idx = idx
            break
    if start_idx is None:
        return None

    opening = lines[start_idx]
    base_indent = len(opening) - len(opening.lstrip())

    end_idx = start_idx
    for idx in range(start_idx + 1, len(lines)):
        line = lines[idx]
        if not line.strip():
            continue  # blank lines belong to whichever block continues after
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        end_idx = idx
    return (start_idx + 1, end_idx + 1)


def anchor_for(path: str, content: str, region: Region) -> Anchor | None:
    """Build an anchor by hashing the region as it currently stands."""
    digest = hash_region(content, region)
    if digest is None:
        return None
    return Anchor(path=path, region=region, content_hash=digest)


def is_stale(anchor: Anchor, tree: TreeSnapshot) -> bool:
    """True when the anchored region is gone or no longer hashes the same."""
    content = tree.get(anchor.path)
    if content is None:
        return True  # the file itself is gone
    current = hash_region(content, anchor.region)
    if current is None:
        return True  # the region is gone
    return current != anchor.content_hash


def stale_anchors(anchors: tuple[Anchor, ...], tree: TreeSnapshot) -> tuple[Anchor, ...]:
    """Every anchor that no longer matches. Empty means the note still applies.

    A note with no anchors is scoped to something the tree cannot contradict
    (`scope="repo"`, a model fact), so it is never stale. That is a real
    limitation and ADR-0007 names it: unanchored notes rely entirely on
    corroboration and refutation.
    """
    return tuple(a for a in anchors if is_stale(a, tree))
