"""Structured results: records, not logs.

Every case produces a record carrying enough to reconstruct the verdict later:
what was asked, what came back, which graders ran and what they said, how long
it took, what it cost, and - critically - the case-set version and the target
version. A pass rate without both versions is a number with no denominator.

Records are written as JSONL so a run streams to disk as it goes and survives
the harness being killed halfway. The summary manifest is separate, because a
summary is derived and must never be the only copy of the evidence.
"""

from __future__ import annotations

import json
import platform
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forge.eval.graders import Grade
from forge.eval.outcomes import Outcome

__all__ = ["CaseRecord", "ResultSet", "RunManifest"]


@dataclass
class CaseRecord:
    """The evidence for one case."""

    case_id: str
    suite: str
    outcome: str
    case_set_version: str
    target_name: str
    target_version: str

    started_at: str = ""
    duration_ms: int = 0
    attempts: int = 1
    seed: int = 0

    # what was asked, and what came back
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)

    grades: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 0
    usd: float = 0.0
    run_id: str | None = None
    error: str | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.outcome == Outcome.PASSED.value

    @classmethod
    def build(
        cls,
        *,
        case: Any,
        outcome: Outcome,
        case_set_version: str,
        target_name: str,
        target_version: str,
        observation: Any = None,
        grades: list[Grade] | None = None,
        duration_ms: int = 0,
        attempts: int = 1,
        seed: int = 0,
        error: str | None = None,
    ) -> CaseRecord:
        return cls(
            case_id=case.id,
            suite=case.suite,
            outcome=outcome.value,
            case_set_version=case_set_version,
            target_name=target_name,
            target_version=target_version,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
            duration_ms=duration_ms,
            attempts=attempts,
            seed=seed,
            input={"goal": case.goal, "tools": list(case.tools), "faults": list(case.faults)},
            output=(
                {
                    "answer": observation.answer,
                    "status": observation.status,
                    "steps": observation.steps,
                    "duplicate_effects": observation.duplicate_effects,
                    "tools_used": observation.tools_used,
                    # Why the target itself stopped, distinct from why the
                    # assertions failed. Without it every failed run is a trip
                    # to the event log.
                    "run_error": (observation.raw or {}).get("run_error"),
                }
                if observation is not None
                else {}
            ),
            grades=[g.to_dict() for g in (grades or [])],
            tokens=getattr(observation, "tokens", 0),
            usd=getattr(observation, "usd", 0.0),
            run_id=getattr(observation, "run_id", None),
            error=error,
            tags=list(case.tags),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunManifest:
    """Everything needed to interpret a set of records."""

    started_at: str
    finished_at: str = ""
    case_set_version: str = ""
    case_set_source: str = ""
    suite: str = ""
    target_name: str = ""
    target_version: str = ""
    harness_version: str = ""
    seed: int = 0
    python: str = field(default_factory=lambda: sys.version.split()[0])
    platform: str = field(default_factory=platform.platform)
    total: int = 0
    counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResultSet:
    """Records plus their manifest."""

    manifest: RunManifest
    records: list[CaseRecord] = field(default_factory=list)

    # -- aggregation -------------------------------------------------------

    def counts(self) -> dict[str, int]:
        return dict(Counter(r.outcome for r in self.records))

    @property
    def verdicts(self) -> list[CaseRecord]:
        """Records that say something about the target.

        Infra noise is excluded on purpose: a pass rate computed over
        unreachable-target records measures the network, not the system.
        """
        return [r for r in self.records if Outcome(r.outcome).is_target_verdict]

    def pass_rate(self) -> float:
        judged = self.verdicts
        if not judged:
            return 0.0
        return round(sum(1 for r in judged if r.passed) / len(judged), 4)

    @property
    def green(self) -> bool:
        """True only if nothing failed and nothing errored.

        Skips do not make a run green-by-omission; they are reported, and a
        suite that skipped everything has a pass rate of zero, not one.
        """
        counts = self.counts()
        bad = sum(
            counts.get(o.value, 0)
            for o in (Outcome.ASSERTION_FAILED, Outcome.HARNESS_ERROR,
                      Outcome.TIMEOUT, Outcome.INFRA_ERROR, Outcome.TARGET_UNAVAILABLE)
        )
        return bad == 0 and counts.get(Outcome.PASSED.value, 0) > 0

    def failures(self) -> list[CaseRecord]:
        return [r for r in self.records if r.outcome == Outcome.ASSERTION_FAILED.value]

    # -- persistence -------------------------------------------------------

    def write(self, directory: str | Path) -> dict[str, Path]:
        """Emit `records.jsonl` + `manifest.json`. Evidence, then summary."""
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)

        self.manifest.total = len(self.records)
        self.manifest.counts = self.counts()

        records_path = out / "records.jsonl"
        with records_path.open("w", encoding="utf-8") as fh:
            for record in self.records:
                fh.write(json.dumps(record.to_dict(), default=str) + "\n")

        manifest_path = out / "manifest.json"
        manifest_path.write_text(
            json.dumps(self.manifest.to_dict(), indent=2, default=str), encoding="utf-8"
        )
        return {"records": records_path, "manifest": manifest_path}

    @classmethod
    def read(cls, directory: str | Path) -> ResultSet:
        """Load a previous run, for regression comparison."""
        out = Path(directory)
        manifest = RunManifest(**json.loads((out / "manifest.json").read_text(encoding="utf-8")))
        records = [
            CaseRecord(**json.loads(line))
            for line in (out / "records.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return cls(manifest=manifest, records=records)

    # -- comparison --------------------------------------------------------

    def compare(self, baseline: ResultSet) -> dict[str, list[str]]:
        """Regressions and fixes against a previous run.

        Only comparable when both ran the same case-set version; otherwise the
        diff conflates "the target got worse" with "the cases changed", which
        is exactly the confusion case-set versioning exists to prevent.
        """
        if baseline.manifest.case_set_version != self.manifest.case_set_version:
            return {
                "incomparable": [
                    f"case-set version differs: baseline "
                    f"{baseline.manifest.case_set_version} vs current "
                    f"{self.manifest.case_set_version}"
                ]
            }
        before = {r.case_id: r.passed for r in baseline.verdicts}
        after = {r.case_id: r.passed for r in self.verdicts}
        return {
            "regressed": sorted(k for k, v in after.items() if before.get(k) and not v),
            "fixed": sorted(k for k, v in after.items() if v and before.get(k) is False),
            "new": sorted(k for k in after if k not in before),
            "removed": sorted(k for k in before if k not in after),
        }
