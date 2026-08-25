"""Unit tests for the pieces the runtime is assembled from."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from forge.context.compiler import ContextCompiler, estimate_tokens
from forge.core.contracts import Action, Effect, Proposal, Usage
from forge.core.enums import Decision, ProposalKind, RetryClass, SideEffect
from forge.errors import DeterministicError, PolicyDenied
from forge.ids import canonical_json, content_hash, idempotency_key
from forge.llm.base import Pricing
from forge.runtime.loopdetect import LoopDetector
from forge.runtime.reconcile import Verdict, reconcile
from forge.runtime.recovery import RetryPolicy, backoff_ms, classify
from forge.security.capabilities import PermitBook
from forge.security.policy import PolicyBundle, PolicyEngine
from forge.state.projection import RunState
from forge.tools.builtin import build_default_registry

# --------------------------------------------------------------- identity


@given(
    st.dictionaries(st.text(max_size=8), st.integers(), max_size=5),
    st.text(max_size=12),
)
@settings(max_examples=100, deadline=None)
def test_idempotency_key_is_stable_under_key_order(args: dict, tool: str) -> None:
    """Same content, different dict ordering -> same key.

    This is the property crash-resume depends on: a resumed worker rebuilds
    the arguments dict from JSON and must land on the identical key.
    """
    shuffled = dict(reversed(list(args.items())))
    assert idempotency_key("run_1", tool, args, 0) == idempotency_key("run_1", tool, shuffled, 0)


def test_idempotency_key_separates_different_actions() -> None:
    a = idempotency_key("run_1", "save", {"name": "x"}, 0)
    b = idempotency_key("run_1", "save", {"name": "y"}, 0)
    c = idempotency_key("run_2", "save", {"name": "x"}, 0)
    d = idempotency_key("run_1", "save", {"name": "x"}, 1)
    assert len({a, b, c, d}) == 4


def test_content_hash_resists_field_boundary_collisions() -> None:
    """('ab','c') and ('a','bc') must not hash alike."""
    assert content_hash("ab", "c") != content_hash("a", "bc")


def test_canonical_json_sorts_keys() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


# -------------------------------------------------------------- contracts


def test_answer_proposal_requires_an_answer() -> None:
    with pytest.raises(ValueError, match="must carry an answer"):
        Proposal(kind=ProposalKind.ANSWER)


def test_tool_call_proposal_requires_a_tool() -> None:
    with pytest.raises(ValueError, match="must name a tool"):
        Proposal(kind=ProposalKind.TOOL_CALL)


def test_contracts_are_immutable() -> None:
    proposal = Proposal(kind=ProposalKind.ANSWER, answer="x")
    with pytest.raises(ValueError):
        proposal.answer = "y"  # type: ignore[misc]


def test_usage_adds() -> None:
    total = Usage(input_tokens=10, output_tokens=5, usd=0.1) + Usage(
        input_tokens=1, output_tokens=2, usd=0.2
    )
    assert (total.input_tokens, total.output_tokens, total.total_tokens) == (11, 7, 18)
    assert total.usd == pytest.approx(0.3)


# ----------------------------------------------------------------- pricing


def test_free_pricing_costs_nothing() -> None:
    assert Pricing().is_free
    assert Pricing().cost(Usage(input_tokens=10_000, output_tokens=10_000)) == 0.0


def test_paid_pricing_is_computed_per_1k() -> None:
    pricing = Pricing(input_per_1k=1.0, output_per_1k=2.0)
    assert not pricing.is_free
    assert pricing.cost(Usage(input_tokens=1000, output_tokens=500)) == pytest.approx(2.0)


# ------------------------------------------------------------- reconcile


def _action(**kw: object) -> Action:
    defaults = dict(
        run_id="run_1", step_id="s1", tool="t", arguments={},
        side_effect=SideEffect.READ, idempotency_key="k1", permit_id="p1",
    )
    defaults.update(kw)
    return Action(**defaults)  # type: ignore[arg-type]


def test_reconcile_matches_successful_effect() -> None:
    verdict = reconcile(_action(), Effect(action_id="a", idempotency_key="k1", ok=True))
    assert verdict.verdict is Verdict.MATCHED


def test_reconcile_flags_key_mismatch() -> None:
    """Evidence for a different action must never be committed."""
    verdict = reconcile(_action(), Effect(action_id="a", idempotency_key="WRONG", ok=True))
    assert verdict.verdict is Verdict.MISMATCH


def test_reconcile_reuse_is_committable() -> None:
    verdict = reconcile(
        _action(), Effect(action_id="a", idempotency_key="k1", ok=True, reused=True)
    )
    assert verdict.verdict is Verdict.REUSED


def test_reconcile_retries_transient_read() -> None:
    verdict = reconcile(
        _action(),
        Effect(action_id="a", idempotency_key="k1", ok=False, retry_class=RetryClass.TRANSIENT),
    )
    assert verdict.verdict is Verdict.RETRYABLE


def test_reconcile_compensates_applied_but_failed_write() -> None:
    """The dangerous case: the write landed, the response did not."""
    verdict = reconcile(
        _action(side_effect=SideEffect.REVERSIBLE_WRITE),
        Effect(
            action_id="a", idempotency_key="k1", ok=False,
            retry_class=RetryClass.TRANSIENT, evidence={"applied": True},
        ),
    )
    assert verdict.verdict is Verdict.NEEDS_COMPENSATION
    assert verdict.compensate is True


def test_reconcile_treats_deterministic_failure_as_benign() -> None:
    verdict = reconcile(
        _action(),
        Effect(
            action_id="a", idempotency_key="k1", ok=False,
            retry_class=RetryClass.DETERMINISTIC,
        ),
    )
    assert verdict.verdict is Verdict.BENIGN_FAILURE


# --------------------------------------------------------------- recovery


def test_classify_is_closed_by_default() -> None:
    class Weird(Exception):
        pass

    assert classify(Weird()) is RetryClass.UNRECOVERABLE
    assert classify(TimeoutError()) is RetryClass.TRANSIENT
    assert classify(ValueError()) is RetryClass.DETERMINISTIC
    assert classify(PolicyDenied("no")) is RetryClass.POLICY_BLOCKED


def test_policy_blocked_is_never_retried() -> None:
    policy = RetryPolicy()
    assert not policy.should_retry(RetryClass.POLICY_BLOCKED, 0)
    assert not policy.should_retry(RetryClass.UNRECOVERABLE, 0)
    assert policy.should_retry(RetryClass.TRANSIENT, 0)


@given(attempt=st.integers(min_value=1, max_value=20))
@settings(max_examples=50, deadline=None)
def test_backoff_is_capped(attempt: int) -> None:
    assert 0 <= backoff_ms(attempt, cap_ms=5000) <= 5000
    assert backoff_ms(attempt, cap_ms=5000, jitter=False) <= 5000


# ---------------------------------------------------------- loop detection


def test_detects_identical_action_repetition() -> None:
    detector = LoopDetector(max_identical=3)
    assert not detector.record_action("aaa")
    assert not detector.record_action("aaa")
    signal = detector.record_action("aaa")
    assert signal and signal.kind == "identical_action"


def test_detects_two_cycle_oscillation() -> None:
    detector = LoopDetector(max_identical=99, max_cycle_repeats=3)
    signal = None
    for fingerprint in ["a", "b"] * 3:
        signal = detector.record_action(fingerprint)
    assert signal and signal.kind == "cyclic_actions"


def test_detects_lack_of_progress() -> None:
    detector = LoopDetector(max_steps_without_progress=3)
    detector.record_step(1)
    assert not detector.record_step(1)
    assert not detector.record_step(1)
    assert detector.record_step(1).kind == "no_progress"


def test_progress_resets_the_stagnation_counter() -> None:
    detector = LoopDetector(max_steps_without_progress=2)
    detector.record_step(1)
    detector.record_step(1)
    assert not detector.record_step(2)  # new observation -> reset


# --------------------------------------------------------------- permits


def test_permit_is_single_use() -> None:
    book = PermitBook()
    permit = book.issue(
        run_id="r", step_id="s", capability="C", action_hash="h", side_effect=SideEffect.READ
    )
    book.redeem(permit.id, action_hash="h")
    with pytest.raises(PolicyDenied, match="already redeemed"):
        book.redeem(permit.id, action_hash="h")


def test_permit_is_bound_to_its_action() -> None:
    """A permit for a cheap action cannot authorize an expensive one."""
    book = PermitBook()
    permit = book.issue(
        run_id="r", step_id="s", capability="C", action_hash="cheap",
        side_effect=SideEffect.READ,
    )
    with pytest.raises(PolicyDenied, match="does not authorize"):
        book.redeem(permit.id, action_hash="expensive")


def test_forged_permit_is_refused() -> None:
    with pytest.raises(PolicyDenied, match="no such permit"):
        PermitBook().redeem("permit_deadbeef", action_hash="h")


# ---------------------------------------------------------------- policy


def _engine(granted: list[str] | None = None) -> PolicyEngine:
    return PolicyEngine(PolicyBundle.zero_cost(granted=granted or []))


def test_policy_denies_tool_outside_task_allow_list() -> None:
    registry = build_default_registry()
    decision = _engine(["KNOWLEDGE_READ"]).authorize_tool(
        spec=registry.get("search_corpus"), arguments={}, task_allow_list=[]
    )
    assert decision.decision is Decision.DENY
    assert "allow-list" in decision.reason


def test_policy_denies_undeclared_capability() -> None:
    registry = build_default_registry()
    decision = _engine([]).authorize_tool(
        spec=registry.get("search_corpus"), arguments={},
        task_allow_list=["search_corpus"],
    )
    assert decision.decision is Decision.DENY
    assert "not declared" in decision.reason


def test_policy_escalates_irreversible_writes() -> None:
    registry = build_default_registry()
    decision = _engine(["EXTERNAL_PUBLISH"]).authorize_tool(
        spec=registry.get("publish"), arguments={}, task_allow_list=["publish"]
    )
    assert decision.decision is Decision.REQUIRE_APPROVAL


def test_paid_inference_is_denied_under_zero_cost_policy() -> None:
    engine = _engine()
    assert engine.authorize_inference(provider_is_free=True).decision is Decision.ALLOW
    assert engine.authorize_inference(provider_is_free=False).decision is Decision.DENY


def test_capability_invocation_ceiling_is_enforced() -> None:
    from forge.security.capabilities import CapabilityGrant

    bundle = PolicyBundle.zero_cost()
    bundle.capabilities["KNOWLEDGE_READ"] = CapabilityGrant(
        name="KNOWLEDGE_READ", granted=True, max_invocations=2,
        allowed_effects=frozenset({SideEffect.READ}),
    )
    registry = build_default_registry()
    engine = PolicyEngine(bundle)
    kwargs = dict(
        spec=registry.get("search_corpus"), arguments={},
        task_allow_list=["search_corpus"],
    )
    assert engine.authorize_tool(**kwargs, invocations_used=1).decision is Decision.ALLOW
    assert engine.authorize_tool(**kwargs, invocations_used=2).decision is Decision.DENY


# ------------------------------------------------------- context compiler


def test_compiler_reports_what_it_dropped() -> None:
    compiler = ContextCompiler(token_budget=120)
    state = RunState(goal="g" * 200)
    state.observations = [{"step": i, "tool": "t", "output": "x" * 500} for i in range(20)]
    view = compiler.compile(step_id="s", state=state, tool_schemas=[])
    assert view.dropped, "an over-budget view must record its drops"
    assert all("budget" in d for d in view.dropped)


def test_compiler_never_drops_the_goal() -> None:
    """Priority floor: the goal and tools survive any budget."""
    compiler = ContextCompiler(token_budget=1)
    state = RunState(goal="find the answer")
    view = compiler.compile(step_id="s", state=state, tool_schemas=[])
    assert any(i.startswith("goal") for i in view.included)
    assert "find the answer" in view.messages[0]["content"]


def test_compiler_is_deterministic() -> None:
    compiler = ContextCompiler(token_budget=2000)
    state = RunState(goal="g", observations=[{"step": 1, "tool": "t", "output": "o"}])
    a = compiler.compile(step_id="s", state=state, tool_schemas=[])
    b = compiler.compile(step_id="s", state=state, tool_schemas=[])
    assert a.snapshot_hash == b.snapshot_hash


def test_compiler_surfaces_failures_and_denials() -> None:
    compiler = ContextCompiler(token_budget=4000)
    state = RunState(goal="g")
    state.failures = [{"step": 1, "kind": "tool_failed", "tool": "x", "detail": "boom"}]
    state.denials = [{"step": 1, "capability": "PUB", "reason": "not granted"}]
    body = compiler.compile(step_id="s", state=state, tool_schemas=[]).messages[0]["content"]
    assert "PREVIOUS FAILURES" in body and "boom" in body
    assert "REFUSED BY POLICY" in body and "not granted" in body


@given(st.text(min_size=0, max_size=500))
@settings(max_examples=50, deadline=None)
def test_token_estimate_is_positive_and_monotonic(text: str) -> None:
    assert estimate_tokens(text) >= 1
    assert estimate_tokens(text + "xxxx") >= estimate_tokens(text)


# ------------------------------------------------------------------ tools


def test_reversible_write_requires_a_compensator() -> None:
    from pydantic import BaseModel

    from forge.tools.registry import ToolSpec

    class Args(BaseModel):
        x: int

    async def fn(x: int) -> int:
        return x

    with pytest.raises(ValueError, match="declares no compensate"):
        ToolSpec(
            name="bad", description="", args_model=Args, fn=fn,
            side_effect=SideEffect.REVERSIBLE_WRITE, capability="C",
        )


def test_unknown_tool_lookup_raises() -> None:
    with pytest.raises(DeterministicError, match="unknown tool"):
        build_default_registry().get("nope")


def test_schemas_respect_the_allow_list() -> None:
    registry = build_default_registry()
    assert registry.schemas([]) == []
    names = [s["name"] for s in registry.schemas(["calculate"])]
    assert names == ["calculate"]


def test_calculator_rejects_non_arithmetic() -> None:
    from forge.tools.builtin import _safe_eval

    with pytest.raises(ValueError):
        _safe_eval("__import__('os').system('echo hi')")
    assert _safe_eval("38 * 2 + 4") == 80
