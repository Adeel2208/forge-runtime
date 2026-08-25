"""Attempts to defeat the trust plane - spec §27.2.

These tests are written from the attacker's side. Each one is a way an agent
(or a prompt injection steering it) might try to reach a capability it was
never granted. They must all fail closed.
"""

from __future__ import annotations

import pytest

from forge.core.contracts import TaskSpec
from forge.core.enums import Decision, EventType, RunStatus, SideEffect
from forge.errors import PolicyDenied
from forge.security.capabilities import PermitBook
from forge.security.policy import PolicyBundle, PolicyEngine
from forge.tools.builtin import WORKSPACE, build_default_registry
from tests.conftest import run

pytestmark = pytest.mark.adversarial

ALL_TOOLS = ["search_corpus", "read_document", "calculate", "save_note", "publish"]


def _spec(tools: list[str] | None = None) -> TaskSpec:
    return TaskSpec(goal="adversarial", tools=tools if tools is not None else ALL_TOOLS)


def test_irreversible_write_is_refused_without_approval(make_runtime) -> None:
    """`publish` is IRREVERSIBLE_WRITE under an ungranted capability."""
    script = [
        {"proposal": {"kind": "TOOL_CALL", "tool": "publish",
                      "arguments": {"destination": "prod", "body": "leak"}}},
        {"proposal": {"kind": "ANSWER", "answer": "blocked"}},
    ]
    runtime = make_runtime(script)

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return result, events

    result, events = run(main())
    assert result.denials, "publish must be denied"
    assert not any(e.type is EventType.ACTION_DISPATCHED and e.payload.get("tool") == "publish"
                   for e in events), "the denied tool must never be dispatched"
    assert result.status is RunStatus.COMPLETED  # denial is survivable, not fatal


def test_tool_outside_the_task_allow_list_cannot_run(make_runtime) -> None:
    """Even a granted capability does not help if the task never asked for it."""
    script = [
        {"proposal": {"kind": "TOOL_CALL", "tool": "save_note",
                      "arguments": {"name": "sneaky", "content": "x"}}},
        {"proposal": {"kind": "ANSWER", "answer": "denied"}},
    ]
    runtime = make_runtime(script)

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec(tools=["calculate"]))  # save_note not listed
        await runtime.store.close()
        return result

    result = run(main())
    assert result.denials
    assert "allow-list" in result.denials[0]["reason"]
    assert "sneaky" not in WORKSPACE


def test_ungranted_capability_blocks_its_whole_tool(make_runtime) -> None:
    script = [
        {"proposal": {"kind": "TOOL_CALL", "tool": "save_note",
                      "arguments": {"name": "n", "content": "c"}}},
        {"proposal": {"kind": "ANSWER", "answer": "done"}},
    ]
    runtime = make_runtime(script, grants=["KNOWLEDGE_READ"])  # no WORKSPACE_WRITE

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec(tools=["save_note"]))
        await runtime.store.close()
        return result

    result = run(main())
    assert result.denials
    assert WORKSPACE == {}, "no write may have occurred"


def test_read_capability_cannot_authorize_a_write() -> None:
    """Effect-class confusion: a READ grant must not cover a write."""
    from forge.security.capabilities import CapabilityGrant

    bundle = PolicyBundle.zero_cost()
    bundle.capabilities["WORKSPACE_WRITE"] = CapabilityGrant(
        name="WORKSPACE_WRITE", granted=True,
        allowed_effects=frozenset({SideEffect.READ}),  # read only
    )
    registry = build_default_registry()
    decision = PolicyEngine(bundle).authorize_tool(
        spec=registry.get("save_note"), arguments={}, task_allow_list=["save_note"]
    )
    assert decision.decision is Decision.DENY
    assert "does not permit" in decision.reason


def test_a_permit_cannot_be_replayed() -> None:
    book = PermitBook()
    permit = book.issue(run_id="r", step_id="s", capability="C",
                        action_hash="h", side_effect=SideEffect.REVERSIBLE_WRITE)
    book.redeem(permit.id, action_hash="h")
    with pytest.raises(PolicyDenied):
        book.redeem(permit.id, action_hash="h")


def test_a_permit_cannot_be_escalated_to_another_action() -> None:
    """Issue for a cheap read, attempt to redeem for an expensive write."""
    book = PermitBook()
    permit = book.issue(run_id="r", step_id="s", capability="KNOWLEDGE_READ",
                        action_hash="hash_of_search", side_effect=SideEffect.READ)
    with pytest.raises(PolicyDenied, match="does not authorize"):
        book.redeem(permit.id, action_hash="hash_of_publish")


def test_expired_step_permits_are_not_redeemable() -> None:
    book = PermitBook()
    permit = book.issue(run_id="r", step_id="s1", capability="C",
                        action_hash="h", side_effect=SideEffect.READ)
    assert book.expire_step("s1") == 1
    with pytest.raises(PolicyDenied, match="no such permit"):
        book.redeem(permit.id, action_hash="h")


def test_prompt_injection_style_tool_name_is_rejected(make_runtime) -> None:
    """A model naming a tool that does not exist gets a denial, not a crash."""
    script = [
        {"proposal": {"kind": "TOOL_CALL", "tool": "../../etc/passwd", "arguments": {}}},
        {"proposal": {"kind": "TOOL_CALL", "tool": "save_note; rm -rf /", "arguments": {}}},
        {"proposal": {"kind": "ANSWER", "answer": "nothing happened"}},
    ]
    runtime = make_runtime(script)

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec())
        await runtime.store.close()
        return result

    result = run(main())
    assert result.status is RunStatus.COMPLETED
    assert len(result.denials) == 2
    assert WORKSPACE == {}


def test_malicious_tool_arguments_fail_validation(make_runtime) -> None:
    """Arguments are validated against the declared model before dispatch."""
    script = [
        {"proposal": {"kind": "TOOL_CALL", "tool": "calculate",
                      "arguments": {"expression": "__import__('os').system('echo pwned')"}}},
        {"proposal": {"kind": "ANSWER", "answer": "safe"}},
    ]
    runtime = make_runtime(script)

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec(tools=["calculate"]))
        events = await runtime.store.read(result.run_id)
        await runtime.store.close()
        return result, events

    result, events = run(main())
    assert result.status is RunStatus.COMPLETED
    effects = [e for e in events if e.type is EventType.EFFECT_OBSERVED]
    assert effects and effects[0].payload["ok"] is False, "the expression must not evaluate"


def test_budget_cannot_be_exceeded_by_asking_nicely(make_runtime) -> None:
    """Ceilings are counted, not negotiated."""
    from forge.llm.mock import MockProvider, ScriptedTurn

    script = [ScriptedTurn(
        proposal={"kind": "TOOL_CALL", "tool": "calculate", "arguments": {"expression": "1+1"}},
        repeat=40,
    )]
    runtime = make_runtime(MockProvider(script), grants=["CALC"])
    runtime.policy.bundle.budget.max_tool_calls = 3
    runtime.detector.max_identical = 999
    runtime.detector.max_steps_without_progress = 999

    async def main():
        await runtime.store.open()
        result = await runtime.start(_spec(tools=["calculate"]))
        await runtime.store.close()
        return result

    result = run(main())
    assert result.status is RunStatus.FAILED
    assert runtime.policy.bundle.budget.tool_calls <= 4


def test_zero_cost_policy_refuses_paid_inference_end_to_end() -> None:
    engine = PolicyEngine(PolicyBundle.zero_cost())
    assert engine.authorize_inference(provider_is_free=False).decision is Decision.DENY
    assert engine.bundle.budget.max_usd == 0.0
