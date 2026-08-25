"""The Context Compiler (spec §10).

Not "concatenate the history". Context is *compiled*: each candidate section
is scored, admitted in priority order until the token budget is spent, and
whatever is dropped is recorded with a reason. The resulting `ContextView` is
hashed so replay can prove the model saw identical input before comparing
behaviour.

Priorities are ordered by what a long-horizon run actually needs. Note that
`failures` outranks older `observations`: a run that keeps re-attempting a
call that already failed is the single most expensive mistake an agent makes,
and the cheapest way to prevent it is to keep the failure in view.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forge.core.contracts import ContextView
from forge.ids import content_hash
from forge.state.projection import RunState

__all__ = ["ContextCompiler", "Section"]


def estimate_tokens(text: str) -> int:
    """Cheap, provider-neutral estimate: ~4 characters per token.

    Deliberately not a tokenizer call - the compiler must stay deterministic
    and dependency-free. It is an estimate used for budgeting, and it errs
    high, which is the safe direction.
    """
    return max(1, (len(text) + 3) // 4)


@dataclass
class Section:
    key: str
    priority: int
    render: str
    role: str = "user"


class ContextCompiler:
    """Builds bounded views from canonical state."""

    SYSTEM_PROMPT = (
        "You are the reasoning core of FORGE, a durable agent runtime.\n"
        "You do not act. You PROPOSE exactly one operation; the runtime "
        "validates, authorizes and executes it.\n"
        "\n"
        "Reply with a single JSON object and nothing else:\n"
        '  {"kind": "TOOL_CALL", "tool": "<name>", "arguments": {...}, '
        '"rationale_summary": "<one short sentence>"}\n'
        '  {"kind": "ANSWER", "answer": "<final answer>", '
        '"rationale_summary": "<one short sentence>"}\n'
        "\n"
        "Rules:\n"
        "- Only use tools listed in AVAILABLE TOOLS. Any other name is refused.\n"
        "- Never repeat a tool call that already appears in OBSERVATIONS.\n"
        "- Never retry a call listed in PREVIOUS FAILURES with identical arguments.\n"
        "- rationale_summary is a public summary, not private reasoning.\n"
        "- When you have enough information, answer. Do not pad with extra calls."
    )

    def __init__(self, *, token_budget: int = 3000, max_observations: int = 8) -> None:
        self.token_budget = token_budget
        self.max_observations = max_observations

    def compile(
        self,
        *,
        step_id: str,
        state: RunState,
        tool_schemas: list[dict[str, Any]],
        budget_note: str = "",
    ) -> ContextView:
        sections = self._candidates(state, tool_schemas, budget_note)
        sections.sort(key=lambda s: s.priority)

        included: list[str] = []
        dropped: list[str] = []
        body: list[str] = []
        spent = estimate_tokens(self.SYSTEM_PROMPT)

        for section in sections:
            cost = estimate_tokens(section.render)
            if spent + cost > self.token_budget and section.priority > 20:
                dropped.append(f"{section.key}(-{cost}t: budget)")
                continue
            spent += cost
            included.append(f"{section.key}(+{cost}t)")
            body.append(section.render)

        messages = [{"role": "user", "content": "\n\n".join(body)}]
        return ContextView(
            step_id=step_id,
            system=self.SYSTEM_PROMPT,
            messages=messages,
            tool_schemas=tool_schemas,
            token_estimate=spent,
            token_budget=self.token_budget,
            included=included,
            dropped=dropped,
            snapshot_hash=content_hash(self.SYSTEM_PROMPT, messages, tool_schemas)[:16],
        )

    def _candidates(
        self, state: RunState, tool_schemas: list[dict[str, Any]], budget_note: str
    ) -> list[Section]:
        sections = [
            Section("goal", 10, f"# GOAL\n{state.goal}"),
            Section("tools", 20, self._render_tools(tool_schemas)),
        ]

        if state.failures:
            sections.append(
                Section("failures", 30, self._render_failures(state.failures[-4:]))
            )
        if state.denials:
            lines = "\n".join(
                f"- {d.get('capability')}: {d.get('reason')}" for d in state.denials[-3:]
            )
            sections.append(
                Section(
                    "denials",
                    35,
                    "# REFUSED BY POLICY\nThese were blocked. Do not retry them.\n" + lines,
                )
            )
        if state.observations:
            sections.append(
                Section(
                    "observations",
                    40,
                    self._render_observations(state.observations[-self.max_observations :]),
                )
            )
        if budget_note:
            sections.append(Section("budget", 50, f"# BUDGET\n{budget_note}"))

        sections.append(
            Section("instruction", 60, "# NOW\nPropose exactly one next operation as JSON.")
        )
        return sections

    @staticmethod
    def _render_tools(schemas: list[dict[str, Any]]) -> str:
        if not schemas:
            return "# AVAILABLE TOOLS\n(none - you must ANSWER from what you already know)"
        lines = ["# AVAILABLE TOOLS"]
        for schema in schemas:
            params = schema.get("parameters", {}).get("properties", {})
            arg_list = ", ".join(
                f"{k}: {v.get('type', 'any')}" for k, v in params.items()
            )
            lines.append(
                f"- {schema['name']}({arg_list}) "
                f"[{schema.get('side_effect', 'READ')}] - {schema.get('description', '')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _render_observations(observations: list[dict[str, Any]]) -> str:
        lines = ["# OBSERVATIONS (results you already have - do not re-request)"]
        for obs in observations:
            output = str(obs.get("output"))
            if len(output) > 600:
                output = output[:600] + "...[truncated]"
            lines.append(f"- step {obs.get('step')} {obs.get('tool')} -> {output}")
        return "\n".join(lines)

    @staticmethod
    def _render_failures(failures: list[dict[str, Any]]) -> str:
        lines = ["# PREVIOUS FAILURES (do not repeat these verbatim)"]
        for failure in failures:
            lines.append(
                f"- step {failure.get('step')} {failure.get('kind')}"
                f"{' ' + str(failure.get('tool')) if failure.get('tool') else ''}"
                f": {str(failure.get('detail'))[:300]}"
            )
        return "\n".join(lines)
