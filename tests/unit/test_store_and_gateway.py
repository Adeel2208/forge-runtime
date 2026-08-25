"""Event store durability and gateway routing."""

from __future__ import annotations

import pytest

from forge.core.contracts import Checkpoint, Usage
from forge.core.enums import EventType
from forge.core.events import NewEvent
from forge.errors import BudgetExhausted, PolicyDenied, ProviderUnavailable
from forge.llm.base import ModelRequest, Pricing
from forge.llm.gateway import CostLedger, LLMGateway
from forge.llm.mock import MockProvider, ScriptedTurn
from forge.state.projection import RunState, project
from forge.state.sqlite_store import SQLiteEventStore
from tests.conftest import run

REQUEST = ModelRequest(system="s", messages=[{"role": "user", "content": "hi"}])


# ------------------------------------------------------------ event store


def test_append_assigns_monotonic_sequence(store_path) -> None:
    async def main() -> list[int]:
        store = SQLiteEventStore(store_path)
        await store.open()
        seqs = []
        for i in range(5):
            result = await store.append(
                NewEvent(type=EventType.STEP_STARTED, run_id="r", payload={"i": i})
            )
            seqs.append(result.event.seq)
        await store.close()
        return seqs

    seqs = run(main())
    assert seqs == sorted(seqs) and len(set(seqs)) == 5


def test_idempotency_key_is_enforced_by_the_store(store_path) -> None:
    """A second append under the same key must not create a second row.

    This is the primitive the whole crash-safety argument rests on, so it is
    tested at the store level rather than only through the runtime.
    """

    async def main() -> tuple[bool, bool, int]:
        store = SQLiteEventStore(store_path)
        await store.open()
        event = NewEvent(
            type=EventType.EFFECT_OBSERVED, run_id="r",
            payload={"ok": True, "n": 1}, idempotency_key="key-1",
        )
        first = await store.append(event)
        second = await store.append(
            event.model_copy(update={"payload": {"ok": True, "n": 2}})
        )
        rows = await store.read("r")
        await store.close()
        return first.deduplicated, second.deduplicated, len(rows)

    first_dedup, second_dedup, count = run(main())
    assert first_dedup is False
    assert second_dedup is True, "the duplicate must be suppressed"
    assert count == 1, "only one effect row may exist for a key"


def test_dedupe_returns_the_original_payload(store_path) -> None:
    async def main() -> dict:
        store = SQLiteEventStore(store_path)
        await store.open()
        base = NewEvent(
            type=EventType.EFFECT_OBSERVED, run_id="r",
            payload={"n": "first"}, idempotency_key="k",
        )
        await store.append(base)
        again = await store.append(base.model_copy(update={"payload": {"n": "second"}}))
        await store.close()
        return again.event.payload

    assert run(main())["n"] == "first", "the winner's payload is authoritative"


def test_idempotency_keys_are_scoped_per_run(store_path) -> None:
    async def main() -> int:
        store = SQLiteEventStore(store_path)
        await store.open()
        for run_id in ("run_a", "run_b"):
            await store.append(
                NewEvent(
                    type=EventType.EFFECT_OBSERVED, run_id=run_id,
                    payload={}, idempotency_key="same-key",
                )
            )
        total = len(await store.read("run_a")) + len(await store.read("run_b"))
        await store.close()
        return total

    assert run(main()) == 2


def test_read_after_seq_returns_only_the_tail(store_path) -> None:
    async def main() -> list[int]:
        store = SQLiteEventStore(store_path)
        await store.open()
        for i in range(6):
            await store.append(
                NewEvent(type=EventType.STEP_STARTED, run_id="r", payload={"i": i})
            )
        tail = await store.read("r", after_seq=3)
        await store.close()
        return [e.payload["i"] for e in tail]

    assert run(main()) == [3, 4, 5]


def test_latest_checkpoint_wins_on_watermark(store_path) -> None:
    async def main() -> int:
        store = SQLiteEventStore(store_path)
        await store.open()
        for step, seq in ((1, 10), (2, 25), (3, 40)):
            await store.write_checkpoint(
                Checkpoint(run_id="r", step_index=step, last_seq=seq, state={"step_index": step})
            )
        latest = await store.latest_checkpoint("r")
        await store.close()
        assert latest is not None
        return latest.step_index

    assert run(main()) == 3


def test_checkpoint_absent_for_unknown_run(store_path) -> None:
    async def main() -> object:
        store = SQLiteEventStore(store_path)
        await store.open()
        result = await store.latest_checkpoint("nope")
        await store.close()
        return result

    assert run(main()) is None


def test_store_rejects_use_before_open(store_path) -> None:
    store = SQLiteEventStore(store_path)
    with pytest.raises(RuntimeError, match="not open"):
        run(store.read("r"))


# --------------------------------------------------------------- projection


def test_projection_is_forward_compatible(store_path) -> None:
    """An unknown event type advances the watermark and is otherwise ignored."""
    from datetime import UTC, datetime

    from forge.core.events import Event

    unknown = Event(
        seq=7, ts=datetime.now(UTC), type=EventType.FAULT_INJECTED,
        run_id="r", payload={"whatever": True},
    )
    state = project([unknown], RunState(run_id="r"))
    assert state.last_seq == 7


def test_projection_folds_effects_into_observations() -> None:
    from datetime import UTC, datetime

    from forge.core.events import Event

    events = [
        Event(seq=1, ts=datetime.now(UTC), type=EventType.RUN_CREATED,
              run_id="r", payload={"goal": "g"}),
        Event(seq=2, ts=datetime.now(UTC), type=EventType.EFFECT_OBSERVED, run_id="r",
              step_index=1, idempotency_key="k1", payload={"ok": True, "tool": "t", "output": "o"}),
        Event(seq=3, ts=datetime.now(UTC), type=EventType.STEP_COMMITTED,
              run_id="r", step_index=1, payload={}),
    ]
    state = project(events)
    assert state.goal == "g"
    assert state.steps_committed == 1
    assert state.observations == [{"step": 1, "tool": "t", "output": "o"}]
    assert "k1" in state.completed_effects


def test_run_state_round_trips_through_dict() -> None:
    state = RunState(
        run_id="r", goal="g", step_index=3, usage=Usage(input_tokens=5, output_tokens=2)
    )
    state.observations.append({"step": 1, "tool": "t", "output": "o"})
    restored = RunState.from_dict(state.to_dict())
    assert restored.to_dict() == state.to_dict()


# ------------------------------------------------------------------ gateway


class _PaidProvider:
    name = "paid"
    model = "expensive-1"
    pricing = Pricing(input_per_1k=10.0, output_per_1k=30.0)

    def __init__(self) -> None:
        self.called = 0

    async def healthy(self) -> bool:
        return True

    async def complete(self, request):
        self.called += 1
        raise AssertionError("this provider must never be reached in this test")


def test_unaffordable_provider_is_never_called() -> None:
    """Spend is checked before the request leaves the process.

    A run must not discover it is over budget by having already gone over.
    """
    paid = _PaidProvider()
    gateway = LLMGateway(providers=[paid], ledger=CostLedger(usd_ceiling=0.01))
    with pytest.raises(BudgetExhausted, match="remaining budget"):
        run(gateway.complete(REQUEST))
    assert paid.called == 0, "an unaffordable provider must not be contacted"


def test_gateway_falls_back_when_the_preferred_provider_is_unaffordable() -> None:
    """Budget pressure degrades into routing, not into an outage."""
    paid, cheap = _PaidProvider(), MockProvider.answering("hello")
    gateway = LLMGateway(providers=[paid, cheap], ledger=CostLedger(usd_ceiling=0.01))
    response = run(gateway.complete(REQUEST))
    assert response.provider == "mock"
    assert paid.called == 0


def test_an_affordable_provider_is_reached() -> None:
    """The gate is a budget, not a ban: with headroom, the call is made."""
    gateway = LLMGateway(providers=[_PaidProvider()], ledger=CostLedger(usd_ceiling=1000.0))
    with pytest.raises(AssertionError, match="never be reached"):
        run(gateway.complete(REQUEST))


def test_gateway_falls_back_on_provider_failure() -> None:
    flaky = MockProvider([ScriptedTurn(raise_timeout=True)], name="flaky")
    good = MockProvider.answering("recovered", name="backup")
    gateway = LLMGateway(providers=[flaky, good])
    assert run(gateway.complete(REQUEST)).provider == "backup"


def test_gateway_records_the_whole_route() -> None:
    attempts: list = []
    flaky = MockProvider([ScriptedTurn(raise_timeout=True)], name="flaky")
    good = MockProvider.answering("ok", name="backup")
    gateway = LLMGateway(providers=[flaky, good], on_attempt=attempts.append)
    run(gateway.complete(REQUEST))
    assert [a.provider for a in attempts][-1] == "backup"
    assert any(not a.ok for a in attempts), "failed attempts must be visible"


def test_gateway_raises_when_everything_fails() -> None:
    dead = MockProvider([ScriptedTurn(raise_timeout=True)], name="dead")
    with pytest.raises(ProviderUnavailable, match="all providers failed"):
        run(LLMGateway(providers=[dead]).complete(REQUEST))


def test_token_ceiling_stops_further_calls() -> None:
    ledger = CostLedger(token_ceiling=10, usd_ceiling=10.0)
    ledger.spent_tokens = 10
    gateway = LLMGateway(providers=[MockProvider.answering("x")], ledger=ledger)
    with pytest.raises(PolicyDenied, match="token ceiling"):
        run(gateway.complete(REQUEST))


def test_ledger_accumulates_usage() -> None:
    gateway = LLMGateway(providers=[MockProvider.answering("x")])
    run(gateway.complete(REQUEST))
    run(gateway.complete(REQUEST))
    assert gateway.ledger.calls == 2
    assert gateway.ledger.spent_tokens > 0
    assert gateway.ledger.remaining_usd == gateway.ledger.usd_ceiling


# -------------------------------------------------------------- mock provider


def test_mock_provider_is_deterministic() -> None:
    script = [{"proposal": {"kind": "ANSWER", "answer": "same"}}]
    first = run(MockProvider(script).complete(REQUEST))
    second = run(MockProvider(script).complete(REQUEST))
    assert first.text == second.text


def test_mock_provider_emits_truncated_json_when_malformed() -> None:
    provider = MockProvider([ScriptedTurn(proposal={"kind": "ANSWER", "answer": "x" * 40},
                                          malformed=True)])
    response = run(provider.complete(REQUEST))
    assert response.parsed is None
    with pytest.raises(ValueError):
        import json

        json.loads(response.text)


def test_mock_provider_repeat_expands_turns() -> None:
    provider = MockProvider([ScriptedTurn(proposal={"kind": "ANSWER", "answer": "a"}, repeat=3)])
    assert len(provider._turns) == 3
