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
            self.workspace, token_budget=(self.config.budget.max_tokens and 6000) or 6000
        )

        started = time.monotonic()
        async with forge:
            runtime = forge._build_runtime()
            runtime.compiler = compiler
            # Two identical edits, not three. A repeated code edit that
            # *succeeds* is worse than one that fails: each application lands,
            # so waiting for a third leaves three copies in the file.
            runtime.detector.max_identical = 2
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

    def __init__(self, workspace: Workspace, *, token_budget: int = 6000) -> None:
        super().__init__(token_budget=token_budget, max_observations=6)
        self.SYSTEM_PROMPT = CODING_SYSTEM_PROMPT
        self._map = workspace.repo_map()

    def _candidates(self, state: Any, tool_schemas: Any, budget_note: str) -> Any:
        from forge.context.compiler import Section

        sections = super()._candidates(state, tool_schemas, budget_note)
        # Priority 15: after the goal, before the tool list. The model needs to
        # know what exists before it can be told what it may do.
        sections.append(Section("repo_map", 15, self._map))
        return sections


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
