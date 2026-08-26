"""Graders: the assertions, kept out of the engine.

The runner knows how to execute a case. It does not know what "correct" means
- that lives here, selected by name from the case data. Adding a new kind of
check is a new grader plus a line of YAML, never an edit to the runner.

Graders are pure: `(case, observation) -> Grade`. They perform no I/O, with
the deliberate exception of `llm_judge`, which is why that one records its
reasoning alongside its score - a subjective verdict you cannot inspect is not
evidence.

A grader whose precondition is missing returns `applicable=False` rather than
passing. Silently passing when a driver could not supply a trajectory is how a
suite comes to report green on checks it never actually ran.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from forge.eval.cases import Case
from forge.eval.targets import Observation

__all__ = ["GRADERS", "Grade", "Grader", "build_grader", "grade_all", "register_grader"]


@dataclass
class Grade:
    """One assertion's verdict."""

    kind: str
    passed: bool
    reason: str = ""
    score: float | None = None
    applicable: bool = True
    reasoning: str | None = None
    """Free-text justification. Required for subjective graders."""

    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "passed": self.passed,
            "reason": self.reason,
            "applicable": self.applicable,
        }
        if self.score is not None:
            out["score"] = self.score
        if self.reasoning:
            out["reasoning"] = self.reasoning
        if self.detail:
            out["detail"] = self.detail
        return out


@runtime_checkable
class Grader(Protocol):
    kind: str

    async def grade(self, case: Case, observation: Observation) -> Grade: ...


# ───────────────────────────────────────────────────────────── text graders


@dataclass
class ContainsGrader:
    value: str
    ignore_case: bool = True
    kind: str = "contains"

    async def grade(self, case: Case, observation: Observation) -> Grade:
        del case
        text = observation.answer or ""
        haystack = text.lower() if self.ignore_case else text
        needle = self.value.lower() if self.ignore_case else self.value
        found = needle in haystack
        return Grade(
            kind=self.kind,
            passed=found,
            reason=(
                f"answer contains {self.value!r}" if found
                else f"answer does not contain {self.value!r}"
            ),
            detail={"answer_excerpt": text[:280]} if not found else {},
        )


@dataclass
class NotContainsGrader:
    value: str
    ignore_case: bool = True
    kind: str = "not_contains"

    async def grade(self, case: Case, observation: Observation) -> Grade:
        inner = await ContainsGrader(self.value, self.ignore_case).grade(case, observation)
        return Grade(
            kind=self.kind,
            passed=not inner.passed,
            reason=(
                f"answer must not contain {self.value!r}" if inner.passed
                else f"answer correctly omits {self.value!r}"
            ),
            detail=inner.detail,
        )


@dataclass
class EqualsGrader:
    value: str
    strip: bool = True
    kind: str = "equals"

    async def grade(self, case: Case, observation: Observation) -> Grade:
        del case
        got = observation.answer or ""
        if self.strip:
            got = got.strip()
        ok = got == self.value
        return Grade(
            kind=self.kind, passed=ok,
            reason="exact match" if ok else f"expected {self.value!r}, got {got[:120]!r}",
        )


@dataclass
class RegexGrader:
    value: str
    kind: str = "regex"

    async def grade(self, case: Case, observation: Observation) -> Grade:
        del case
        try:
            pattern = re.compile(self.value, re.IGNORECASE | re.DOTALL)
        except re.error as exc:
            return Grade(kind=self.kind, passed=False, reason=f"invalid pattern: {exc}")
        ok = bool(pattern.search(observation.answer or ""))
        return Grade(
            kind=self.kind, passed=ok,
            reason=f"pattern {self.value!r} {'matched' if ok else 'did not match'}",
        )


@dataclass
class JsonSchemaGrader:
    """Validate the answer as JSON against a schema.

    Deliberately a small structural check rather than a full JSON Schema
    implementation - it covers type, required keys and nested objects, and
    says so plainly instead of pretending to be a validator it is not.
    """

    value: dict[str, Any]
    kind: str = "json_schema"

    async def grade(self, case: Case, observation: Observation) -> Grade:
        del case
        raw = (observation.answer or "").strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return Grade(
                kind=self.kind, passed=False,
                reason=f"answer is not valid JSON: {exc}",
                detail={"answer_excerpt": raw[:280]},
            )
        problems = _check_schema(payload, self.value, path="$")
        return Grade(
            kind=self.kind,
            passed=not problems,
            reason="conforms to schema" if not problems else "; ".join(problems[:4]),
            detail={"problems": problems} if problems else {},
        )


def _check_schema(value: Any, schema: dict[str, Any], *, path: str) -> list[str]:
    problems: list[str] = []
    expected = schema.get("type")
    types: dict[str, Any] = {
        "object": dict, "array": list, "string": str,
        "number": (int, float), "integer": int, "boolean": bool,
    }
    if expected and expected in types and not isinstance(value, types[expected]):
        return [f"{path}: expected {expected}, got {type(value).__name__}"]

    if expected == "object" or isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}: missing required key {key!r}")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value and isinstance(sub, dict):
                problems += _check_schema(value[key], sub, path=f"{path}.{key}")
    elif expected == "array" and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value[:20]):
            problems += _check_schema(item, schema["items"], path=f"{path}[{i}]")
    return problems


# ─────────────────────────────────────────────────────── budget/status graders


@dataclass
class TerminalStatusGrader:
    value: str = "COMPLETED"
    kind: str = "terminal_status"

    async def grade(self, case: Case, observation: Observation) -> Grade:
        del case
        ok = observation.status.upper() == str(self.value).upper()
        return Grade(
            kind=self.kind, passed=ok,
            reason=f"status {observation.status} (expected {self.value})",
        )


@dataclass
class _NumericCeiling:
    value: float
    kind: str = "max"
    attribute: str = "steps"
    label: str = "steps"

    async def grade(self, case: Case, observation: Observation) -> Grade:
        del case
        actual = getattr(observation, self.attribute, 0)
        ok = actual <= self.value
        return Grade(
            kind=self.kind, passed=ok,
            reason=f"{self.label}={actual} (ceiling {self.value})",
            detail={} if ok else {self.label: actual, "ceiling": self.value},
        )


def _max_steps(value: Any) -> Grader:
    return _NumericCeiling(float(value), kind="max_steps", attribute="steps", label="steps")


def _max_tokens(value: Any) -> Grader:
    return _NumericCeiling(float(value), kind="max_tokens", attribute="tokens", label="tokens")


def _max_usd(value: Any) -> Grader:
    return _NumericCeiling(float(value), kind="max_usd", attribute="usd", label="usd")


def _max_duration_ms(value: Any) -> Grader:
    return _NumericCeiling(
        float(value), kind="max_duration_ms", attribute="duration_ms", label="duration_ms"
    )


# ───────────────────────────────────────────────────── trajectory graders


@dataclass
class NoDuplicateEffectsGrader:
    value: bool = True
    kind: str = "no_duplicate_effects"

    async def grade(self, case: Case, observation: Observation) -> Grade:
        del case
        count = observation.duplicate_effects
        ok = (count == 0) if self.value else (count > 0)
        return Grade(
            kind=self.kind, passed=ok,
            reason=f"{count} duplicate external effect(s)",
            detail={} if ok else {"duplicate_effects": count},
        )


@dataclass
class ToolUsedGrader:
    value: Any
    expected: bool = True
    kind: str = "tool_used"

    async def grade(self, case: Case, observation: Observation) -> Grade:
        del case
        if not observation.has_trajectory:
            return Grade(
                kind=self.kind, passed=False, applicable=False,
                reason="driver supplied no trajectory; check not run",
            )
        wanted = [self.value] if isinstance(self.value, str) else list(self.value)
        used = set(observation.tools_used)
        missing = [t for t in wanted if t not in used]
        ok = (not missing) if self.expected else all(t not in used for t in wanted)
        return Grade(
            kind=self.kind, passed=ok,
            reason=(
                f"tools used: {sorted(used) or 'none'}"
                if ok else f"expected {'' if self.expected else 'no '}{wanted}, "
                f"used {sorted(used) or 'none'}"
            ),
        )


@dataclass
class ToolNotUsedGrader:
    value: Any
    kind: str = "tool_not_used"

    async def grade(self, case: Case, observation: Observation) -> Grade:
        inner = await ToolUsedGrader(self.value, expected=False).grade(case, observation)
        return Grade(
            kind=self.kind, passed=inner.passed,
            reason=inner.reason, applicable=inner.applicable,
        )


@dataclass
class PolicyDeniedGrader:
    value: Any = True
    kind: str = "policy_denied"

    async def grade(self, case: Case, observation: Observation) -> Grade:
        del case
        if not observation.has_trajectory:
            return Grade(
                kind=self.kind, passed=False, applicable=False,
                reason="driver supplied no trajectory; check not run",
            )
        denials = observation.denials
        if isinstance(self.value, str):
            ok = any(self.value in str(d.get("capability") or "") for d in denials)
            reason = f"denials for {self.value!r}: {ok}"
        else:
            ok = bool(denials) is bool(self.value)
            reason = f"{len(denials)} policy denial(s)"
        return Grade(kind=self.kind, passed=ok, reason=reason,
                     detail={"denials": denials[:4]} if denials else {})


# ────────────────────────────────────────────────────────────── LLM judge


@dataclass
class LlmJudgeGrader:
    """Rubric grading by a model, for genuinely subjective criteria.

    Records the judge's reasoning and the model identity with the score. A
    subjective verdict without its justification is not reviewable, and an
    unreviewable verdict should not gate a release.
    """

    value: str
    threshold: float = 0.7
    provider: Any = None
    kind: str = "llm_judge"

    async def grade(self, case: Case, observation: Observation) -> Grade:
        if self.provider is None:
            return Grade(
                kind=self.kind, passed=False, applicable=False,
                reason="no judge provider configured; check not run",
            )
        from forge.llm.base import ModelRequest

        rubric = self.value
        request = ModelRequest(
            system=(
                "You are grading one answer against a rubric. Reply with JSON only: "
                '{"score": <0.0-1.0>, "reasoning": "<two sentences>"}. '
                "Score strictly; a partially correct answer scores below 0.5."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"TASK\n{case.goal}\n\nRUBRIC\n{rubric}\n\n"
                    f"ANSWER\n{observation.answer or '(no answer)'}"
                ),
            }],
            response_schema={
                "type": "object",
                "properties": {
                    "score": {"type": "number"},
                    "reasoning": {"type": "string"},
                },
                "required": ["score", "reasoning"],
            },
            max_tokens=300,
        )
        try:
            response = await self.provider.complete(request)
        except Exception as exc:
            return Grade(
                kind=self.kind, passed=False, applicable=False,
                reason=f"judge unavailable: {type(exc).__name__}: {exc}",
            )

        payload = response.parsed or {}
        if not payload:
            try:
                payload = json.loads(response.text)
            except json.JSONDecodeError:
                return Grade(
                    kind=self.kind, passed=False, applicable=False,
                    reason="judge returned unparseable output",
                )
        score = float(payload.get("score", 0.0))
        reasoning = str(payload.get("reasoning", ""))
        return Grade(
            kind=self.kind,
            passed=score >= self.threshold,
            score=score,
            reason=f"score {score:.2f} (threshold {self.threshold:.2f})",
            reasoning=reasoning,
            detail={"judge_model": f"{response.provider}/{response.model}"},
        )


# ──────────────────────────────────────────────────────────────── registry


GRADERS: dict[str, Any] = {
    "contains": lambda v, **kw: ContainsGrader(str(v), **kw),
    "not_contains": lambda v, **kw: NotContainsGrader(str(v), **kw),
    "equals": lambda v, **kw: EqualsGrader(str(v), **kw),
    "regex": lambda v, **kw: RegexGrader(str(v), **kw),
    "json_schema": lambda v, **kw: JsonSchemaGrader(dict(v), **kw),
    "terminal_status": lambda v, **kw: TerminalStatusGrader(str(v), **kw),
    "max_steps": lambda v, **kw: _max_steps(v),
    "max_tokens": lambda v, **kw: _max_tokens(v),
    "max_usd": lambda v, **kw: _max_usd(v),
    "max_duration_ms": lambda v, **kw: _max_duration_ms(v),
    "no_duplicate_effects": lambda v, **kw: NoDuplicateEffectsGrader(bool(v)),
    "tool_used": lambda v, **kw: ToolUsedGrader(v),
    "tool_not_used": lambda v, **kw: ToolNotUsedGrader(v),
    "policy_denied": lambda v, **kw: PolicyDeniedGrader(v),
    "llm_judge": lambda v, **kw: LlmJudgeGrader(str(v), **kw),
}


def register_grader(kind: str, factory: Any) -> None:
    """Add a grader without touching the runner. That is the whole point."""
    GRADERS[kind] = factory


class UnknownGrader(KeyError):
    """A case names a grader this build does not have."""


def build_grader(spec: dict[str, Any], *, judge_provider: Any = None) -> Grader:
    kind = str(spec.get("type") or "").strip()
    if kind not in GRADERS:
        raise UnknownGrader(
            f"unknown grader {kind!r}; available: {', '.join(sorted(GRADERS))}"
        )
    options = {k: v for k, v in spec.items() if k not in ("type", "value")}
    if kind == "llm_judge" and judge_provider is not None:
        options["provider"] = judge_provider
    return GRADERS[kind](spec.get("value"), **options)  # type: ignore[no-any-return]


async def grade_all(
    case: Case, observation: Observation, *, judge_provider: Any = None
) -> list[Grade]:
    """Run every grader a case declares. A case with no graders is an error,
    surfaced here rather than silently passing."""
    if not case.expect:
        return [Grade(
            kind="none", passed=False,
            reason="case declares no expectations; a case that asserts nothing cannot pass",
        )]
    grades: list[Grade] = []
    for spec in case.expect:
        grader = build_grader(spec, judge_provider=judge_provider)
        grades.append(await grader.grade(case, observation))
    return grades
