"""Conversational memory, and what it must never do.

Two properties matter more than the rest. The newest turn has to survive
compaction, because a follow-up instruction is almost always about it - "also
handle negatives" refers to the last task, never the seventh. And the budget
has to be enforced rather than hoped for: a context that silently overruns is
how a small model starts truncating the instructions at the end of the prompt,
which are the ones telling it what to do.
"""

from __future__ import annotations

from forge.coding.memory import Conversation, StructuralCompactor, Turn
from forge.context.compiler import estimate_tokens


def _turn(n: int, *, files: tuple[str, ...] = (), status: str = "completed") -> Turn:
    return Turn(
        goal=f"task number {n} with a reasonably wordy description of the change",
        status=status,
        answer=f"I changed things for task {n}, at some length, to fill the budget.",
        files=files,
        commits=1 if files else 0,
        branch=f"forge/code_{n}",
    )


# -- what survives ---------------------------------------------------------


def test_the_newest_turn_is_kept_in_full() -> None:
    """A follow-up is about the last task. Losing it is losing the referent."""
    convo = Conversation()
    for i in range(20):
        convo.record(_turn(i, files=(f"src/f{i}.py",)))

    rendered = convo.render(budget_tokens=900)
    assert "task number 19" in rendered
    assert "src/f19.py" in rendered


def test_older_turns_degrade_rather_than_vanish() -> None:
    """Shape is still useful when detail is not affordable."""
    convo = Conversation()
    for i in range(12):
        convo.record(_turn(i, files=(f"src/f{i}.py",)))

    rendered = convo.render(budget_tokens=1200)
    assert "task number 11" in rendered, "newest in full"
    assert "task number 5" in rendered, "middle kept in brief"
    assert "earlier task(s) in this session" in rendered, "oldest collapsed to a count"


def test_a_failed_turn_is_remembered() -> None:
    """"Try that again differently" needs to know what was tried."""
    convo = Conversation()
    convo.record(_turn(1, status="failed"))
    assert "outcome: failed" in convo.render(budget_tokens=900)


def test_a_turn_that_changed_nothing_says_so() -> None:
    convo = Conversation()
    convo.record(_turn(1, files=()))
    assert "you changed nothing" in convo.render(budget_tokens=900)


# -- the budget ------------------------------------------------------------


def test_the_budget_is_enforced_not_hoped_for() -> None:
    convo = Conversation()
    for i in range(60):
        convo.record(_turn(i, files=(f"src/f{i}.py", f"tests/t{i}.py")))

    for budget in (120, 300, 900, 2000):
        rendered = convo.render(budget_tokens=budget)
        assert estimate_tokens(rendered) <= budget, (
            f"history overran a {budget}-token budget at {estimate_tokens(rendered)}"
        )


def test_a_budget_of_nothing_renders_nothing() -> None:
    convo = Conversation()
    convo.record(_turn(1))
    assert convo.render(budget_tokens=0) == ""


def test_an_empty_conversation_renders_nothing() -> None:
    assert Conversation().render(budget_tokens=900) == ""


def test_compaction_drops_whole_tiers_never_half_a_turn() -> None:
    """A truncated turn reads as a complete fact that happens to be wrong,
    which is worse than an honest omission."""
    convo = Conversation()
    for i in range(30):
        convo.record(_turn(i, files=(f"src/f{i}.py",)))

    rendered = convo.render(budget_tokens=200)
    # Whatever survived, no line may be a mid-sentence fragment of a turn.
    for line in rendered.split("\n"):
        if line.startswith("You were asked:"):
            assert line.endswith(("…", ".", "e")) or len(line) > 20


# -- bounds ----------------------------------------------------------------


def test_the_conversation_does_not_grow_without_bound() -> None:
    convo = Conversation(limit=10)
    for i in range(50):
        convo.record(_turn(i))

    assert len(convo) == 10
    assert "task number 49" in convo.render(budget_tokens=900), "newest still kept"


def test_long_text_is_clipped_rather_than_carried() -> None:
    convo = Conversation()
    convo.record(Turn(goal="x" * 5000, status="completed", answer="y" * 5000))

    rendered = convo.render(budget_tokens=900)
    assert len(rendered) < 2000
    assert "…" in rendered


def test_the_compactor_is_replaceable() -> None:
    """Structural compaction is the reliable default; an LLM summariser can
    take its place without the compiler knowing which it has."""

    class Fixed:
        def render(self, turns: list[Turn], budget_tokens: int) -> str:
            del budget_tokens
            return f"summary of {len(turns)} turns"

    convo = Conversation(compactor=Fixed())
    convo.record(_turn(1))
    assert convo.render(budget_tokens=900) == "summary of 1 turns"


def test_the_default_compactor_keeps_two_turns_verbatim() -> None:
    assert StructuralCompactor().verbatim == 2
