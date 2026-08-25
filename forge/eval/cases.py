"""Cases as data.

A case set is a versioned YAML/JSON document, not Python. That is what lets
someone who is not a Python programmer add coverage, and what makes the case
set diffable, reviewable and ownable by the people who understand the domain.

    version: 1.0.0
    suite: agent-core
    defaults:
      tools: [search_corpus, read_document]
    cases:
      - id: lookup.context-compilation
        goal: What did FORGE measure about context compilation?
        expect:
          - contains: "38%"
          - max_steps: 5
          - no_duplicate_effects: true

Two rules the loader enforces rather than trusts:

* **IDs are stable and unique.** A result is addressed by case id; duplicates
  silently overwrite each other in every downstream report.
* **The set is versioned.** A pass rate means nothing on its own - it is only
  interpretable as (case-set version x target version), so the version travels
  with every record this set produces.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = ["Case", "CaseSet", "CaseSetError"]

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,80}$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class CaseSetError(ValueError):
    """A case set that cannot be trusted. Raised at load time, never at run time."""


@dataclass(frozen=True)
class Case:
    """One test case. Pure data - it carries no execution logic."""

    id: str
    goal: str
    suite: str = "default"
    tools: tuple[str, ...] = ()
    max_steps: int | None = None
    expect: tuple[dict[str, Any], ...] = ()
    """Grader specifications, resolved by `forge.eval.graders`."""

    fixture: str | None = None
    """Optional scripted model turns, making the case fully deterministic."""

    faults: tuple[str, ...] = ()
    """Fault classes to inject, for resilience cases."""

    tags: tuple[str, ...] = ()
    timeout_s: float = 120.0
    skip: str | None = None
    """Reason for skipping. Present in results as SKIPPED, never silent."""

    def seed_for(self, suite_seed: int) -> int:
        """A per-case seed derived from the case id.

        Derived rather than sequential so a case's seed does not change when
        an unrelated case is inserted before it - otherwise every insertion
        silently re-rolls the whole suite.
        """
        digest = 0
        for char in self.id:
            digest = (digest * 131 + ord(char)) & 0xFFFFFFFF
        return (suite_seed ^ digest) & 0x7FFFFFFF


@dataclass(frozen=True)
class CaseSet:
    """A versioned, ordered collection of cases."""

    version: str
    suite: str
    cases: tuple[Case, ...]
    source: str = "<memory>"
    seed: int = 1729
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- loading -----------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> CaseSet:
        """Load one file, or merge every case file in a directory."""
        target = Path(path)
        if target.is_dir():
            files = sorted(
                p for p in target.iterdir()
                if p.suffix.lower() in (".yaml", ".yml", ".json")
            )
            if not files:
                raise CaseSetError(f"no case files in {target}")
            return cls.merge([cls._load_file(p) for p in files], source=str(target))
        return cls._load_file(target)

    @classmethod
    def _load_file(cls, path: Path) -> CaseSet:
        text = path.read_text(encoding="utf-8")
        try:
            raw = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
        except (ValueError, yaml.YAMLError) as exc:
            raise CaseSetError(f"{path}: cannot parse: {exc}") from exc
        if not isinstance(raw, dict):
            raise CaseSetError(f"{path}: top level must be a mapping")
        return cls.from_dict(raw, source=str(path))

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, source: str = "<memory>") -> CaseSet:
        version = str(raw.get("version", "")).strip()
        if not _SEMVER_RE.match(version):
            raise CaseSetError(
                f"{source}: 'version' must be semver (e.g. 1.0.0); got {version!r}. "
                "A result is only meaningful as (case-set version x target version)."
            )

        suite = str(raw.get("suite") or "default")
        defaults = raw.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise CaseSetError(f"{source}: 'defaults' must be a mapping")

        entries = raw.get("cases")
        if not isinstance(entries, list) or not entries:
            raise CaseSetError(f"{source}: 'cases' must be a non-empty list")

        cases: list[Case] = []
        seen: dict[str, int] = {}
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise CaseSetError(f"{source}: case #{index} is not a mapping")
            case = cls._build_case(entry, defaults, suite, source, index)
            if case.id in seen:
                raise CaseSetError(
                    f"{source}: duplicate case id {case.id!r} "
                    f"(also at #{seen[case.id]}). Case ids address results and "
                    "must be unique across the set."
                )
            seen[case.id] = index
            cases.append(case)

        return cls(
            version=version,
            suite=suite,
            cases=tuple(cases),
            source=source,
            seed=int(raw.get("seed", 1729)),
            metadata=dict(raw.get("metadata") or {}),
        )

    @staticmethod
    def _build_case(
        entry: dict[str, Any], defaults: dict[str, Any], suite: str, source: str, index: int
    ) -> Case:
        merged = {**defaults, **entry}
        case_id = str(merged.get("id", "")).strip()
        if not _ID_RE.match(case_id):
            raise CaseSetError(
                f"{source}: case #{index} has invalid id {case_id!r}; expected "
                "lowercase alphanumeric with . _ - separators"
            )
        goal = str(merged.get("goal", "")).strip()
        if not goal:
            raise CaseSetError(f"{source}: case {case_id!r} has no 'goal'")

        expect = merged.get("expect") or []
        if not isinstance(expect, list):
            raise CaseSetError(f"{source}: case {case_id!r}: 'expect' must be a list")
        normalised: list[dict[str, Any]] = []
        for item in expect:
            if isinstance(item, dict) and len(item) == 1 and "type" not in item:
                # Shorthand: `- contains: "38%"` -> {"type": "contains", "value": "38%"}
                (kind, value), = item.items()
                normalised.append({"type": str(kind), "value": value})
            elif isinstance(item, dict):
                normalised.append(dict(item))
            else:
                raise CaseSetError(
                    f"{source}: case {case_id!r}: each 'expect' entry must be a mapping"
                )

        return Case(
            id=case_id,
            goal=goal,
            suite=str(merged.get("suite") or suite),
            tools=tuple(merged.get("tools") or ()),
            max_steps=merged.get("max_steps"),
            expect=tuple(normalised),
            fixture=merged.get("fixture"),
            faults=tuple(merged.get("faults") or ()),
            tags=tuple(merged.get("tags") or ()),
            timeout_s=float(merged.get("timeout_s", 120.0)),
            skip=merged.get("skip"),
        )

    @classmethod
    def merge(cls, sets: list[CaseSet], *, source: str) -> CaseSet:
        """Combine several files into one addressable set."""
        if not sets:
            raise CaseSetError("nothing to merge")
        cases: list[Case] = []
        seen: set[str] = set()
        for case_set in sets:
            for case in case_set.cases:
                if case.id in seen:
                    raise CaseSetError(
                        f"duplicate case id {case.id!r} across files under {source}"
                    )
                seen.add(case.id)
                cases.append(case)
        versions = sorted({s.version for s in sets})
        return cls(
            version="+".join(versions) if len(versions) > 1 else versions[0],
            suite=sets[0].suite if len({s.suite for s in sets}) == 1 else "mixed",
            cases=tuple(cases),
            source=source,
            seed=sets[0].seed,
            metadata={"files": [s.source for s in sets]},
        )

    # -- selection ---------------------------------------------------------

    def select(
        self, *, ids: list[str] | None = None, tags: list[str] | None = None
    ) -> CaseSet:
        """Filter without losing the version - a subset is still this set."""
        chosen = self.cases
        if ids:
            wanted = set(ids)
            chosen = tuple(c for c in chosen if c.id in wanted)
        if tags:
            wanted_tags = set(tags)
            chosen = tuple(c for c in chosen if wanted_tags & set(c.tags))
        return CaseSet(
            version=self.version, suite=self.suite, cases=chosen,
            source=self.source, seed=self.seed, metadata=self.metadata,
        )

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.cases)
