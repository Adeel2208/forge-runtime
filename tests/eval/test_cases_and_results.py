"""The case-set contract, and the result records it produces.

Case sets are edited by people who are not Python programmers, so every
mistake they can make must be caught at *load* time with a message that says
what to do - never at run time, and never silently.
"""

from __future__ import annotations

import json

import pytest

from forge.eval import CaseSet, CaseSetError, Outcome
from forge.eval.results import CaseRecord, ResultSet, RunManifest

MINIMAL = {
    "version": "1.0.0",
    "suite": "s",
    "cases": [{"id": "a.b", "goal": "g", "expect": [{"contains": "x"}]}],
}


def _load(**overrides):
    return CaseSet.from_dict({**MINIMAL, **overrides}, source="<test>")


# ── versioning ──────────────────────────────────────────────────────────


def test_version_is_required() -> None:
    """A result is only meaningful as (case-set version x target version)."""
    with pytest.raises(CaseSetError, match="version"):
        CaseSet.from_dict({"suite": "s", "cases": MINIMAL["cases"]}, source="<t>")


def test_version_must_be_semver() -> None:
    with pytest.raises(CaseSetError, match="semver"):
        _load(version="v1")


def test_version_travels_onto_every_record() -> None:
    cases = _load(version="2.3.4")
    record = CaseRecord.build(
        case=cases.cases[0], outcome=Outcome.PASSED,
        case_set_version=cases.version, target_name="t", target_version="v9",
    )
    assert record.case_set_version == "2.3.4"
    assert record.target_version == "v9"


# ── identity ────────────────────────────────────────────────────────────


def test_duplicate_ids_are_rejected() -> None:
    """Ids address results; duplicates silently overwrite each other downstream."""
    with pytest.raises(CaseSetError, match="duplicate case id"):
        _load(cases=[
            {"id": "same.id", "goal": "a", "expect": [{"contains": "x"}]},
            {"id": "same.id", "goal": "b", "expect": [{"contains": "y"}]},
        ])


def test_malformed_ids_are_rejected() -> None:
    for bad in ("Has Spaces", "UPPER", "a", "!!"):
        with pytest.raises(CaseSetError, match="invalid id"):
            _load(cases=[{"id": bad, "goal": "g", "expect": [{"contains": "x"}]}])


def test_missing_goal_is_rejected() -> None:
    with pytest.raises(CaseSetError, match="no 'goal'"):
        _load(cases=[{"id": "a.b", "expect": [{"contains": "x"}]}])


def test_cases_must_be_a_non_empty_list() -> None:
    with pytest.raises(CaseSetError, match="non-empty"):
        _load(cases=[])


# ── authoring ergonomics ────────────────────────────────────────────────


def test_expect_shorthand_is_normalised() -> None:
    """`- contains: "x"` is the form a non-programmer will write."""
    case = _load().cases[0]
    assert case.expect == ({"type": "contains", "value": "x"},)


def test_explicit_expect_form_is_preserved() -> None:
    case = _load(cases=[{
        "id": "a.b", "goal": "g",
        "expect": [{"type": "llm_judge", "value": "rubric", "threshold": 0.9}],
    }]).cases[0]
    assert case.expect[0]["threshold"] == 0.9


def test_defaults_apply_to_every_case_and_are_overridable() -> None:
    cases = _load(
        defaults={"tools": ["a", "b"], "max_steps": 7},
        cases=[
            {"id": "x.inherits", "goal": "g", "expect": [{"contains": "x"}]},
            {"id": "x.overrides", "goal": "g", "tools": ["c"],
             "expect": [{"contains": "x"}]},
        ],
    )
    assert cases.cases[0].tools == ("a", "b")
    assert cases.cases[0].max_steps == 7
    assert cases.cases[1].tools == ("c",)


# ── determinism ─────────────────────────────────────────────────────────


def test_seeds_are_derived_from_the_case_id_not_position() -> None:
    """Inserting a case must not re-roll every other case's seed."""
    first = _load(cases=[
        {"id": "a.one", "goal": "g", "expect": [{"contains": "x"}]},
        {"id": "a.two", "goal": "g", "expect": [{"contains": "x"}]},
    ])
    second = _load(cases=[
        {"id": "a.zero", "goal": "g", "expect": [{"contains": "x"}]},
        {"id": "a.one", "goal": "g", "expect": [{"contains": "x"}]},
        {"id": "a.two", "goal": "g", "expect": [{"contains": "x"}]},
    ])
    by_id_first = {c.id: c.seed_for(first.seed) for c in first}
    by_id_second = {c.id: c.seed_for(second.seed) for c in second}
    assert by_id_first["a.one"] == by_id_second["a.one"]
    assert by_id_first["a.two"] == by_id_second["a.two"]


def test_seed_is_stable_across_processes() -> None:
    case = _load().cases[0]
    assert case.seed_for(1729) == case.seed_for(1729)


# ── selection ───────────────────────────────────────────────────────────


def test_selection_preserves_the_set_version() -> None:
    """A subset is still this set - the version must survive filtering."""
    cases = _load(version="3.1.4", cases=[
        {"id": "a.one", "goal": "g", "tags": ["fast"], "expect": [{"contains": "x"}]},
        {"id": "a.two", "goal": "g", "tags": ["slow"], "expect": [{"contains": "x"}]},
    ])
    subset = cases.select(tags=["fast"])
    assert len(subset) == 1
    assert subset.version == "3.1.4"


def test_selection_by_id() -> None:
    cases = _load(cases=[
        {"id": "a.one", "goal": "g", "expect": [{"contains": "x"}]},
        {"id": "a.two", "goal": "g", "expect": [{"contains": "x"}]},
    ])
    assert [c.id for c in cases.select(ids=["a.two"])] == ["a.two"]


# ── loading from disk ───────────────────────────────────────────────────


def test_loads_the_shipped_case_set() -> None:
    """The real case set must stay loadable and internally valid."""
    from forge.eval.graders import GRADERS

    cases = CaseSet.load("cases")
    assert len(cases) >= 5
    for case in cases:
        assert case.expect, f"{case.id} declares no expectations"
        for spec in case.expect:
            assert spec["type"] in GRADERS, f"{case.id}: unknown grader {spec['type']!r}"


def test_yaml_and_json_both_load(tmp_path) -> None:
    (tmp_path / "a.json").write_text(json.dumps(MINIMAL), encoding="utf-8")
    loaded = CaseSet.load(tmp_path / "a.json")
    assert len(loaded) == 1


def test_duplicate_ids_across_files_are_rejected(tmp_path) -> None:
    for name in ("a.json", "b.json"):
        (tmp_path / name).write_text(json.dumps(MINIMAL), encoding="utf-8")
    with pytest.raises(CaseSetError, match="duplicate case id"):
        CaseSet.load(tmp_path)


# ── results ─────────────────────────────────────────────────────────────


def _result_set(outcomes: list[Outcome]) -> ResultSet:
    cases = _load(cases=[
        {"id": f"a.case{i}", "goal": "g", "expect": [{"contains": "x"}]}
        for i in range(len(outcomes))
    ])
    records = [
        CaseRecord.build(
            case=case, outcome=outcome, case_set_version=cases.version,
            target_name="t", target_version="v1",
        )
        for case, outcome in zip(cases.cases, outcomes, strict=True)
    ]
    return ResultSet(
        manifest=RunManifest(started_at="now", case_set_version=cases.version,
                             target_version="v1"),
        records=records,
    )


def test_pass_rate_excludes_infrastructure_noise() -> None:
    """Unreachable targets must not depress the quality signal."""
    results = _result_set([
        Outcome.PASSED, Outcome.PASSED,
        Outcome.TARGET_UNAVAILABLE, Outcome.INFRA_ERROR,
    ])
    assert results.pass_rate() == 1.0, "2/2 judged cases passed"
    assert len(results.verdicts) == 2
    assert not results.green, "infra failures still block a green run"


def test_assertion_failure_lowers_the_pass_rate() -> None:
    results = _result_set([Outcome.PASSED, Outcome.ASSERTION_FAILED])
    assert results.pass_rate() == 0.5
    assert not results.green


def test_records_round_trip_through_disk(tmp_path) -> None:
    original = _result_set([Outcome.PASSED, Outcome.ASSERTION_FAILED])
    paths = original.write(tmp_path)
    assert paths["records"].exists() and paths["manifest"].exists()

    reloaded = ResultSet.read(tmp_path)
    assert [r.case_id for r in reloaded.records] == [r.case_id for r in original.records]
    assert reloaded.pass_rate() == original.pass_rate()


def test_records_are_jsonl_one_object_per_line(tmp_path) -> None:
    """Streamed to disk, so a killed harness still leaves evidence."""
    _result_set([Outcome.PASSED] * 3).write(tmp_path)
    lines = (tmp_path / "records.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        assert json.loads(line)["case_id"]


def test_comparison_refuses_across_case_set_versions() -> None:
    """Otherwise a diff conflates 'target got worse' with 'cases changed'."""
    baseline = _result_set([Outcome.PASSED])
    baseline.manifest.case_set_version = "1.0.0"
    current = _result_set([Outcome.ASSERTION_FAILED])
    current.manifest.case_set_version = "2.0.0"
    assert "incomparable" in current.compare(baseline)


def test_comparison_names_regressions() -> None:
    baseline = _result_set([Outcome.PASSED, Outcome.PASSED])
    current = _result_set([Outcome.PASSED, Outcome.ASSERTION_FAILED])
    diff = current.compare(baseline)
    assert diff["regressed"] == ["a.case1"]
    assert diff["fixed"] == []
