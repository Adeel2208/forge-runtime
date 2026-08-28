"""Conversational memory for the coding agent.

Without this, every task starts cold: "now also handle negative numbers" has no
idea what "also" refers to, and the agent re-reads files it read a minute ago
to rediscover what it already decided.

Three things make this different from appending a chat log to the prompt.

**A turn is a record of something that happened, not a message.** It carries
the goal, the outcome, the files touched and the branch - facts already in the
event log and in git, which is why a turn can be *reconstructed* rather than
stored separately and trusted. Nothing here is a second source of truth.

**Compaction is deterministic.** The obvious approach is to ask a model to
summarise the older turns, and on a local 2B model that is a call that can be
slow, wrong, or silently drop the one detail that mattered. Compaction here is
structural: recent turns keep their detail, older ones fall back to goal and
outcome, the oldest collapse to a count. It cannot fail, it costs nothing, and
it is testable - and an LLM summariser can be dropped in later behind
`Compactor` for the cases where prose genuinely helps.

**The budget is enforced, not hoped for.** A context that silently overruns is
how a small model starts truncating the instructions at the end of the prompt,
which are the ones that tell it what to do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from forge.context.compiler import estimate_tokens

__all__ = ["Compactor", "Conversation", "StructuralCompactor", "Turn"]

MAX_GOAL_CHARS = 300
MAX_ANSWER_CHARS = 400


@dataclass(frozen=True)
class Turn:
    """One completed exchange: what was asked, and what actually happened."""

    goal: str
    status: str
    answer: str = ""
    files: tuple[str, ...] = ()
    commits: int = 0
    branch: str = ""

    @property
    def changed_anything(self) -> bool:
        return self.commits > 0

    def full(self) -> str:
        """Everything worth keeping about this turn."""
        lines = [f"You were asked: {_clip(self.goal, MAX_GOAL_CHARS)}"]
        if self.files:
            lines.append(f"  you changed: {', '.join(self.files[:8])}")
        elif self.status == "completed":
            lines.append("  you changed nothing")
        if self.answer:
            lines.append(f"  you said: {_clip(self.answer, MAX_ANSWER_CHARS)}")
        if self.status != "completed":
            lines.append(f"  outcome: {self.status}")
        return "\n".join(lines)

    def brief(self) -> str:
        """The same turn, reduced to what a later turn can still act on."""
        what = ", ".join(self.files[:4]) if self.files else "no files"
        return f"- {_clip(self.goal, 110)} -> {self.status}, {what}"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Compactor(Protocol):
    """Turns a history into text that fits a budget.

    A protocol so an LLM summariser can replace the structural one without the
    compiler knowing which it has.
    """

    def render(self, turns: list[Turn], budget_tokens: int) -> str: ...


@dataclass
class StructuralCompactor:
    """Tiered compaction: detail for the recent, shape for the rest.

    Recency is what a follow-up instruction actually refers to - "also handle
    negatives" is about the last turn, never the seventh - so the newest turns
    keep their detail and older ones degrade rather than disappear. A turn that
    changed files is kept in brief even when it is old, because it is the one a
    later instruction is most likely to be talking about.
    """

    verbatim: int = 2
    """Newest turns rendered in full."""

    briefly: int = 6
    """Older turns rendered as one line each."""

    def render(self, turns: list[Turn], budget_tokens: int) -> str:
        if not turns or budget_tokens <= 0:
            return ""

        recent = turns[-self.verbatim :]
        middle = turns[-(self.verbatim + self.briefly) : -self.verbatim]
        oldest = turns[: -(self.verbatim + self.briefly)] if len(turns) > (
            self.verbatim + self.briefly
        ) else []

        blocks: list[str] = []
        if oldest:
            changed = sum(1 for t in oldest if t.changed_anything)
            blocks.append(
                f"({len(oldest)} earlier task(s) in this session, {changed} of which "
                f"changed files. Ask if you need their detail.)"
            )
        if middle:
            blocks.append("\n".join(t.brief() for t in middle))
        if recent:
            blocks.append("\n\n".join(t.full() for t in recent))

        body = "\n\n".join(b for b in blocks if b)
        rendered = f"# THIS SESSION SO FAR\n{body}"

        # Enforced, not hoped for: drop whole tiers oldest-first until it fits.
        # Truncating mid-turn would leave a half-sentence the model reads as
        # fact, which is worse than an honest omission.
        while estimate_tokens(rendered) > budget_tokens and blocks:
            blocks.pop(0)
            body = "\n\n".join(b for b in blocks if b)
            rendered = f"# THIS SESSION SO FAR\n{body}"

        if not blocks:
            return ""
        return rendered


@dataclass
class Conversation:
    """The turns of one Studio session, newest last.

    Bounded: a session that has run two hundred tasks does not need all of them
    in memory, and the compactor would discard them anyway.
    """

    turns: list[Turn] = field(default_factory=list)
    limit: int = 60
    compactor: Compactor = field(default_factory=StructuralCompactor)

    def record(self, turn: Turn) -> None:
        self.turns.append(turn)
        del self.turns[: -self.limit]

    def render(self, budget_tokens: int) -> str:
        return self.compactor.render(self.turns, budget_tokens)

    def __len__(self) -> int:
        return len(self.turns)
