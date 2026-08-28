"""The coding agent.

What makes this different from pointing a general agent at a repo:

**A repo map, not a file dump.** A small model cannot hold a codebase in
context, and filling the window with source it did not ask for is the fastest
way to make it worse. It gets a structural map and opens what it chooses.

**One operation per step.** FORGE's proposal model was already this, and for
an 8B model it is an advantage rather than a constraint: parallel tool calls
are where small models fall apart.

**Git as compensation.** Each committed step is a git commit on a run branch.
A mis-applied edit is restored by the runtime; a bad run is one `branch -D`
from never having happened.

**Test output as the feedback loop.** `run_tests` is a READ so the agent can
use it freely, and its result lands in the context for the next step.

None of this makes a small model as capable as a frontier one. It makes a
small model's failures *cheap*, which is a different and achievable goal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.coding.git import GitRepo, GitSession
from forge.coding.tools import CodingContext, build_coding_registry
from forge.coding.workspace import Workspace
from forge.config import ForgeConfig
from forge.context.compiler import ContextCompiler
from forge.core.contracts import TaskSpec
from forge.core.enums import EventType, RunStatus
from forge.deployment import Forge
from forge.ids import new_id
from forge.runtime.loop import RunResult
from forge.security.policy import PolicyBundle
from forge.telemetry.logging import get_logger

__all__ = ["CODING_SYSTEM_PROMPT", "CodingAgent", "CodingResult"]

log = get_logger("forge.coding")

PACKAGED_POLICY = Path(__file__).parent / "coding-policy.yaml"


CODING_SYSTEM_PROMPT = (
    "You are a careful software engineer working inside one repository.\n"
    "You propose exactly ONE operation per step. The runtime executes it and "
    "shows you the result.\n"
    "\n"
    "Reply with a single JSON object and nothing else:\n"
    '  {"kind":"TOOL_CALL","tool":"<name>","arguments":{...},'
    '"rationale_summary":"<one short sentence>"}\n'
    '  {"kind":"ANSWER","answer":"<what you changed and why>",'
    '"rationale_summary":"<one short sentence>"}\n'
    "\n"
    "How to work:\n"
    "1. Use the REPOSITORY MAP to pick a file. Do not guess paths.\n"
    "2. read_file before you edit it. Never edit a file you have not read "
    "this run.\n"
    "3. Prefer edit_file over write_file. old_text must be copied EXACTLY "
    "from what you read, including indentation, and must appear only once.\n"
    "4. Make one small change at a time. A failed big edit costs more than "
    "three small ones.\n"
    "5. run_tests after changing behaviour. Read the failure before editing "
    "again.\n"
    "6. ANSWER when the task is done, summarising what you changed.\n"
    "\n"
    "Rules:\n"
    "- Only tools in AVAILABLE TOOLS exist. Anything else is refused.\n"
    "- Do not repeat a call already in OBSERVATIONS.\n"
    "- Do not retry something in PREVIOUS FAILURES with identical arguments.\n"
    "- Your edits are on a scratch git branch and are reviewed before merge, "
    "so prefer making progress over asking permission."
)


@dataclass
class CodingResult:
    """Outcome of a coding run, plus what to do about it."""

    run: RunResult
    branch: str | None
    base_ref: str
    commits: int
    files_touched: list[str] = field(default_factory=list)
    diff_stat: str = ""
    tests_run: int = 0
    tests_passed: bool | None = None
    review_hint: str = ""

    @property
    def ok(self) -> bool:
        return self.run.status is RunStatus.COMPLETED

    @property
    def changed_anything(self) -> bool:
        return self.commits > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.run.to_dict(),
            "branch": self.branch,
            "base_ref": self.base_ref,
            "commits": self.commits,
            "files_touched": self.files_touched,
            "tests_run": self.tests_run,
            "tests_passed": self.tests_passed,
        }


class CodingAgent:
    """Runs one coding task against one repository."""

    def __init__(
        self,
        repo_path: str | Path = ".",
        *,
        config: ForgeConfig | None = None,
        policy: PolicyBundle | None = None,
        allow_dirty: bool = False,
        branch_prefix: str = "forge",
        approval: Any = None,
        sandbox: Any = None,
    ) -> None:
        self.workspace = Workspace(Path(repo_path))
        self.repo = GitRepo(self.workspace.root)
        self.config = config or ForgeConfig.load()
        self.allow_dirty = allow_dirty
        self.branch_prefix = branch_prefix
        self.approval = approval
        self._sandbox = sandbox
        self.isolation: Any = sandbox.isolation if sandbox is not None else None
        self._policy = policy or self._load_policy()

    def _load_policy(self) -> PolicyBundle:
        bundle = PolicyBundle.from_yaml(PACKAGED_POLICY)
        budget = self.config.budget
        bundle.budget.max_usd = budget.max_usd
        bundle.budget.max_tokens = budget.max_tokens
        # Coding needs more steps than a question-answering task: read, edit,
        # test, read the failure, edit again is already five.
        bundle.budget.max_steps = max(budget.max_steps, 40)
        bundle.budget.max_tool_calls = max(budget.max_tool_calls, 60)
        return bundle

    async def run(
        self,
        task: str,
        *,
        max_steps: int | None = None,
        on_step: Any = None,
    ) -> CodingResult:
        """Execute a coding task. Returns what changed and how to review it."""
        run_id = new_id("code")
        session = GitSession(self.repo, run_id, branch_prefix=self.branch_prefix)
        session.start(allow_dirty=self.allow_dirty)

        # Pick the strongest sandbox this machine offers, then tell the policy
        # engine what it got. A capability that requires more than is available
        # stays denied - the decision is made by comparison, not by judgement.
        from forge.sandbox import Isolation, select_sandbox

        sandbox = self._sandbox or await select_sandbox(
            workspace_root=self.workspace.root, minimum=Isolation.CONFINED
        )
        self.isolation = sandbox.isolation

        context = CodingContext(self.workspace, self.repo, sandbox=sandbox)
        registry = build_coding_registry(context)

        forge = Forge(
            config=self.config,
            registry=registry,
            policy=self._policy,
            available_isolation=sandbox.isolation.label,
            approval=self.approval,
        )
        compiler = _CodingCompiler(
            self.workspace,
            repo=self.repo,
            base_ref=session.base_ref,
            token_budget=6000,
        )

        started = time.monotonic()
        async with forge:
            runtime = forge._build_runtime()
            runtime.compiler = compiler
            # Two identical *edits*, not three: each application lands, so
            # waiting for a third leaves three copies in the file. Reads keep
            # the looser default - re-reading a file mid-task is ordinary, and
            # bounding both at two killed legitimate runs.
            runtime.detector.max_identical_write = 2
            if on_step is not None:
                _attach_progress(runtime, session, context, on_step)
            else:
                _attach_commits(runtime, session, context)

            spec = TaskSpec(
                goal=task,
                tools=registry.names(),
                max_steps=max_steps or self._policy.budget.max_steps,
            )
            result = await runtime.start(spec, run_id=run_id)

        summary = session.summary()
        log.info(
            "coding run finished",
            run_id=run_id,
            status=result.status.value,
            commits=summary.get("commits"),
            files=len(context.files_touched),
            took_ms=int((time.monotonic() - started) * 1000),
        )
        return CodingResult(
            run=result,
            branch=session.branch or None,
            base_ref=session.base_ref,
            commits=len(session.commits),
            files_touched=sorted(context.files_touched),
            diff_stat=str(summary.get("diff_stat") or ""),
            tests_run=context.tests_run,
            tests_passed=context.tests_passed,
            review_hint=session.review_hint(),
        )


class _CodingCompiler(ContextCompiler):
    """Context compiler that leads with a repository map.

    The map is computed once per run, not per step: a coding agent's file
    layout barely changes mid-task, and recomputing it every step would spend
    the token budget on something the model already saw.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        repo: Any = None,
        base_ref: str = "",
        token_budget: int = 6000,
        max_diff_chars: int = 2500,
    ) -> None:
        super().__init__(token_budget=token_budget, max_observations=6)
        self.SYSTEM_PROMPT = CODING_SYSTEM_PROMPT
        self._map = workspace.repo_map()
        self._repo = repo
        self._base_ref = base_ref
        self._max_diff_chars = max_diff_chars

    def _candidates(self, state: Any, tool_schemas: Any, budget_note: str) -> Any:
        from forge.context.compiler import Section

        sections = super()._candidates(state, tool_schemas, budget_note)
        # Priority 15: after the goal, before the tool list. The model needs to
        # know what exists before it can be told what it may do.
        sections.append(Section("repo_map", 15, self._map))

        work = self._work_so_far()
        if work:
            # Priority 12 - above even the repo map. A small model that cannot
            # see its own changes re-does them: it added the same function
            # three times, and stopped after one file believing it was done.
            # Its own diff is the cheapest possible correction, and unlike a
            # prompt instruction the model cannot forget to consult it.
            sections.append(Section("work_so_far", 12, work))

        # Replace the generic closing instruction. The base compiler asks
        # whether the observations satisfy the goal - which for a coding task
        # a model answers "yes" after merely *reading* the file, and the run
        # then completes having changed nothing. Reading is never the work
        # here, and the closing instruction is the last thing read before
        # generating, so it is where that has to be said.
        sections = [s for s in sections if s.key != "instruction"]
        if work:
            closing = (
                "# NOW - DECIDE\n"
                "The diff above is everything you have changed so far.\n"
                "- If it fully satisfies the GOAL, reply "
                '{"kind": "ANSWER", "answer": "<what you changed>"}.\n'
                "- Otherwise call edit_file or write_file to make the next change.\n"
                "Do not re-read a file you have already read, and do not repeat "
                "an edit that already appears in the diff."
            )
        else:
            closing = (
                "# NOW - DECIDE\n"
                "You have not changed anything yet, so the GOAL is not met.\n"
                "Reading a file is not the work: call edit_file or write_file "
                "to make the change the GOAL asks for.\n"
                "Answer only once the change has actually been made."
            )
        sections.append(Section("instruction", 60, closing))
        return sections

    def _work_so_far(self) -> str:
        """The cumulative diff for this run, bounded."""
        if self._repo is None or not self._base_ref:
            return ""
        try:
            diff = self._repo.diff(base=self._base_ref)
            # `git diff` says nothing about untracked files, so a file the
            # agent just *created* is invisible in its own running diff -
            # exactly the case where it is most likely to create it twice.
            created = [
                line[3:].strip()
                for line in self._repo.run("status", "--porcelain").splitlines()
                if line.startswith("??")
            ]
        except Exception:
            return ""

        if created:
            diff += "\n\n# files you created (not yet committed):\n" + "\n".join(
                f"  {name}" for name in created[:20]
            )
        if not diff.strip():
            return ""

        if len(diff) > self._max_diff_chars:
            # Keep the head: hunk headers and the first changes carry more
            # signal than the tail of a long diff.
            diff = diff[: self._max_diff_chars] + "\n... [diff truncated]"

        return (
            "# WORK YOU HAVE ALREADY DONE IN THIS TASK\n"
            "These changes are already applied and committed. Do NOT repeat "
            "them. Move on to the parts of the task not yet done.\n\n"
            f"{diff}"
        )


def _attach_commits(runtime: Any, session: GitSession, context: CodingContext) -> None:
    """Commit after each committed step, by wrapping the checkpoint hook.

    Hooking `_checkpoint` rather than the tool call is deliberate: a step is
    committed to the event log at exactly that point, so the git history and
    the event log stay in step with each other.
    """
    original = runtime._checkpoint

    async def checkpoint_and_commit(run_id: str, state: Any) -> None:
        await original(run_id, state)
        if context.files_touched:
            last = state.observations[-1] if state.observations else {}
            summary = str(last.get("output") or "changes")
            session.commit_step(state.step_index, summary)

    runtime._checkpoint = checkpoint_and_commit


def _attach_progress(
    runtime: Any, session: GitSession, context: CodingContext, on_step: Any
) -> None:
    """Commit and report progress, for an interactive front end."""
    _attach_commits(runtime, session, context)

    # Effects are appended straight to the store rather than through `_emit`,
    # because the append *is* the idempotency claim and carries the key that
    # `_emit` has no parameter for. Widening the runtime's most safety-critical
    # path for the sake of a progress line would be the wrong trade, so
    # completion is reported from the commit hook instead - which is also the
    # more honest signal: it fires when the step is durably recorded, not when
    # a tool merely returned.
    after_commit = runtime._checkpoint

    async def checkpoint_and_report(run_id: str, state: Any) -> None:
        before = len(state.observations)
        await after_commit(run_id, state)
        if state.observations:
            last = state.observations[-1]
            on_step(
                EventType.STEP_COMMITTED,
                {
                    "tool": last.get("tool"),
                    "index": state.step_index,
                    "ok": True,
                    "new": len(state.observations) != before,
                },
            )

    runtime._checkpoint = checkpoint_and_report
    original = runtime._emit

    async def emit_and_report(type_: EventType, run_id: str, **kwargs: Any) -> int:
        seq: int = await original(type_, run_id, **kwargs)
        if type_ in (
            EventType.PROPOSAL_RECEIVED,
            EventType.EFFECT_OBSERVED,
            EventType.POLICY_DECIDED,
            EventType.RETRY_SCHEDULED,
        ):
            on_step(type_, kwargs.get("payload") or {})
        return seq

    runtime._emit = emit_and_report
