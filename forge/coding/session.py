"""The interactive session: `forge` with no arguments.

A coding tool is a conversation, not a one-shot command. You describe
something, watch it work, look at what it did, and either keep it or say what
was wrong. This is that loop.

Two things here are not cosmetic:

**Approval finally has a human.** The runtime has always been able to return
`REQUIRE_APPROVAL` for an irreversible action, but every entry point until now
either auto-approved or refused. A session is the one place a person is
actually present, so this is where that gate gets answered.

**Accept and undo are explicit.** Each task produces a branch. Nothing merges
into your work unless you say so, and `undo` is a real operation rather than
advice in a help text.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.coding.agent import CodingAgent, CodingResult
from forge.coding.git import GitError, NotARepository
from forge.coding.ui import accent, bold, dim, error, header, ok, rule, warn
from forge.coding.ui import field as ui_field
from forge.core.enums import EventType
from forge.errors import ForgeError

__all__ = ["Session", "run_session"]

BANNER_HELP = """\
  Type what you want done, in plain language.

  /diff            what the last task changed
  /accept          merge the last task into your branch
  /undo            discard the last task's branch
  /status          repo, model and sandbox
  /policy          what this agent may and may not do
  /trace           the event log for the last run
  /history         tasks in this session
  /help            this
  /quit            leave (nothing is merged unless you accepted it)
"""


@dataclass
class Turn:
    """One task and what came of it."""

    task: str
    result: CodingResult | None = None
    accepted: bool = False
    discarded: bool = False

    @property
    def status(self) -> str:
        if self.result is None:
            return "failed to start"
        if self.discarded:
            return "discarded"
        if self.accepted:
            return "accepted"
        return self.result.run.status.value.lower()


@dataclass
class Session:
    """An interactive coding session over one repository."""

    repo: Path = field(default_factory=Path)
    allow_dirty: bool = False
    turns: list[Turn] = field(default_factory=list)
    agent: CodingAgent | None = None
    sandbox: Any = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> bool:
        """Build the agent. False if the repository is unusable.

        The sandbox is chosen here rather than per run so the banner and
        `/policy` can report the tier that will actually apply - a capability
        shown as granted while it is really blocked by isolation is exactly
        the kind of confident-but-wrong display this project keeps removing.
        """
        from forge.sandbox import Isolation, select_sandbox

        try:
            self.sandbox = await select_sandbox(minimum=Isolation.CONFINED)
            self.agent = CodingAgent(
                self.repo, allow_dirty=self.allow_dirty,
                approval=self._ask_approval, sandbox=self.sandbox,
            )
        except (NotARepository, GitError, ForgeError) as exc:
            print(f"\n  {error(str(exc))}\n")
            return False
        return True

    async def loop(self) -> int:
        """The REPL. Returns a process exit code."""
        if not await self.start():
            return 2

        self._banner()
        while True:
            try:
                # On a worker thread: a blocking `input()` would hold the
                # event loop for as long as the user is thinking, stalling
                # anything running in the background.
                raw = (await asyncio.to_thread(input, bold(accent("\n> ")))).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not raw:
                continue
            if raw.startswith("/"):
                if await self._command(raw):
                    break
                continue

            await self._task(raw)

        self._farewell()
        return 0

    # -- banner and status -------------------------------------------------

    def _banner(self) -> None:
        assert self.agent
        print()
        print(header("FORGE", "interactive coding session"))
        print(rule())
        self._status()
        print(dim("\n  /help for commands. Nothing merges into your branch "
                  "unless you /accept it."))

    def _status(self) -> None:
        assert self.agent
        agent = self.agent
        providers = agent.config.providers
        model = f"{providers[0].kind}/{providers[0].model}" if providers else "none"

        try:
            branch = agent.repo.current_branch()
            clean = "clean" if agent.repo.is_clean() else warn("uncommitted changes")
        except GitError:
            branch, clean = "?", "?"

        sandbox = self.sandbox.isolation.label if self.sandbox else dim("unknown")

        print(ui_field("repo", str(agent.workspace.root)))
        print(ui_field("branch", f"{branch}  {clean}"))
        print(ui_field("model", model))
        print(ui_field("sandbox", str(sandbox)))
        print(ui_field("policy", agent._policy.version))

    # -- running a task ----------------------------------------------------

    async def _task(self, task: str) -> None:
        assert self.agent
        turn = Turn(task=task)
        self.turns.append(turn)
        print()

        try:
            turn.result = await self.agent.run(task, on_step=self._progress)
        except (NotARepository, GitError, ForgeError) as exc:
            print(f"  {error(str(exc))}")
            return
        except KeyboardInterrupt:
            # The run holds a lease and a branch; both survive the interrupt,
            # so this is a pause rather than a loss.
            print(f"\n  {warn('interrupted')} - partial work is on the branch")
            return

        self._report(turn.result)

    def _progress(self, event: EventType, payload: dict[str, Any]) -> None:
        match event:
            case EventType.PROPOSAL_RECEIVED:
                tool = payload.get("tool")
                if tool:
                    args = payload.get("arguments") or {}
                    detail = (
                        args.get("path") or args.get("pattern")
                        or args.get("target") or args.get("command") or ""
                    )
                    print(f"  {dim('.')} {accent(str(tool).ljust(13))} {dim(str(detail)[:52])}")
                else:
                    print(f"  {dim('.')} {accent('answering')}")
            case EventType.POLICY_DECIDED if payload.get("decision") == "DENY":
                print(f"    {warn('refused')} {dim(str(payload.get('reason'))[:60])}")
            case EventType.EFFECT_OBSERVED if not payload.get("ok"):
                print(f"    {warn('failed')}  {dim(str(payload.get('error'))[:60])}")
            case EventType.RETRY_SCHEDULED:
                print(f"    {dim('retrying')}")
            case _:
                pass

    def _report(self, result: CodingResult) -> None:
        print()
        status = ok("completed") if result.ok else error(result.run.status.value.lower())
        print(ui_field("status", status))
        print(ui_field("steps", str(result.run.steps)))

        if result.files_touched:
            shown = ", ".join(result.files_touched[:5])
            extra = f" (+{len(result.files_touched) - 5})" if len(result.files_touched) > 5 else ""
            print(ui_field("files", shown + extra))
        if result.tests_run:
            verdict = ok("passing") if result.tests_passed else error("FAILING")
            print(ui_field("tests", f"ran {result.tests_run}x, {verdict}"))
        if result.run.denials:
            print(ui_field("refused", warn(f"{len(result.run.denials)} action(s) by policy")))
        if result.run.error:
            print(ui_field("error", dim(result.run.error[:70])))

        if result.changed_anything:
            print(ui_field("branch", f"{result.branch}  ({result.commits} commits)"))
            print(f"\n  {result.run.answer or ''}")
            print(dim("\n  /diff to review | /accept to keep | /undo to discard"))
        else:
            print(f"\n  {result.run.answer or dim('nothing changed')}")

    # -- human in the loop -------------------------------------------------

    async def _ask_approval(self, action: Any) -> bool:
        """The approval gate, answered by the person sitting here.

        Defaults to *no*: an operator who hits return without reading should
        get the safe outcome, and an irreversible action is exactly where that
        matters.
        """
        print()
        print(f"  {warn('approval required')}")
        print(ui_field("tool", str(action.tool)))
        print(ui_field("effect", str(action.side_effect.value)))
        for key, value in list(action.arguments.items())[:4]:
            print(ui_field(f"  {key}", str(value)[:60]))

        try:
            answer = await asyncio.to_thread(input, f"  {bold('allow? [y/N] ')}")
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        approved = answer.strip().lower() in ("y", "yes")
        print(f"  {ok('approved') if approved else warn('refused')}")
        return approved

    # -- commands ----------------------------------------------------------

    async def _command(self, raw: str) -> bool:
        """Run a slash command. Returns True to exit the session."""
        assert self.agent
        name, _, argument = raw[1:].partition(" ")
        name = name.lower()
        last = self._last_with_result()

        match name:
            case "quit" | "q" | "exit":
                return True

            case "help" | "h" | "?":
                print(f"\n{BANNER_HELP}")

            case "status":
                print()
                self._status()

            case "diff" | "d":
                if last is None or last.result is None or not last.result.changed_anything:
                    print(dim("\n  nothing to show"))
                else:
                    print()
                    print(self.agent.repo.diff(base=last.result.base_ref) or dim("  empty diff"))

            case "accept" | "a":
                self._accept(last)

            case "undo" | "u":
                self._undo(last)

            case "policy":
                self._policy()

            case "trace":
                await self._trace(last, argument)

            case "history":
                self._history()

            case _:
                print(dim(f"\n  unknown command /{name} - try /help"))
        return False

    def _last_with_result(self) -> Turn | None:
        for turn in reversed(self.turns):
            if turn.result is not None and not turn.discarded:
                return turn
        return None

    def _accept(self, turn: Turn | None) -> None:
        assert self.agent
        if turn is None or turn.result is None or not turn.result.changed_anything:
            print(dim("\n  nothing to accept"))
            return
        result = turn.result
        try:
            original = self.agent.repo.current_branch()
            if original == result.branch:
                # The agent left us on its own branch; go back before merging.
                self.agent.repo.checkout(_base_branch(self.agent, result))
            self.agent.repo.run("merge", "--no-ff", "-m",
                                f"forge: {turn.task[:60]}", str(result.branch))
        except GitError as exc:
            print(f"\n  {error(f'merge failed: {exc}')}")
            print(dim(f"  the branch is intact: {result.branch}"))
            return
        turn.accepted = True
        print(f"\n  {ok('merged')} {result.branch}")

    def _undo(self, turn: Turn | None) -> None:
        assert self.agent
        if turn is None or turn.result is None or not turn.result.changed_anything:
            print(dim("\n  nothing to undo"))
            return
        result = turn.result
        try:
            if self.agent.repo.current_branch() == result.branch:
                self.agent.repo.checkout(_base_branch(self.agent, result))
            self.agent.repo.delete_branch(str(result.branch))
        except GitError as exc:
            print(f"\n  {error(f'could not delete the branch: {exc}')}")
            return
        turn.discarded = True
        print(f"\n  {ok('discarded')} {result.branch} - your work is untouched")

    def _policy(self) -> None:
        assert self.agent
        bundle = self.agent._policy
        print(f"\n  {bold(bundle.version)}")
        rank = {"none": 0, "confined": 1, "container": 2}
        have = rank.get(self.sandbox.isolation.label, 0) if self.sandbox else 0

        for name, grant in sorted(bundle.capabilities.items()):
            needs = rank.get(grant.requires_isolation, 0)
            blocked = grant.granted and needs > have
            if blocked:
                mark = warn("BLOCKED")
            elif grant.granted:
                mark = ok("granted")
            else:
                mark = warn("DENIED ")

            notes = []
            if blocked:
                notes.append(
                    f"needs {grant.requires_isolation} isolation, "
                    f"this machine has {self.sandbox.isolation.label}"
                )
            elif grant.requires_isolation != "none":
                notes.append(f"under {grant.requires_isolation}")
            if grant.requires_approval:
                notes.append("needs approval")
            suffix = dim("  " + ", ".join(notes)) if notes else ""
            print(f"    [{mark}] {name}{suffix}")

    async def _trace(self, turn: Turn | None, argument: str) -> None:
        """The audit trail for a run, read from the durable log."""
        assert self.agent
        if turn is None or turn.result is None:
            print(dim("\n  no run to trace"))
            return

        run_id = argument.strip() or turn.result.run.run_id
        print(dim(f"\n  {run_id}"))

        from forge.state.sqlite_store import SQLiteEventStore

        store = SQLiteEventStore(self.agent.config.sqlite_path)
        await store.open()
        try:
            events = await store.read(run_id)
        finally:
            await store.close()

        if not events:
            print(dim("    no events recorded"))
            return
        for event in events:
            if event.type is not EventType.PHASE_ENTERED:
                print(dim("    " + event.summary()))

    def _history(self) -> None:
        if not self.turns:
            print(dim("\n  nothing yet"))
            return
        print()
        for index, turn in enumerate(self.turns, start=1):
            mark = {
                "accepted": ok("+"), "discarded": dim("-"),
                "completed": accent("."),
            }.get(turn.status, warn("!"))
            print(f"  {mark} {index}. {turn.task[:58]}  {dim(turn.status)}")

    def _farewell(self) -> None:
        kept = [t for t in self.turns if t.accepted]
        pending = [
            t for t in self.turns
            if t.result and t.result.changed_anything and not t.accepted and not t.discarded
        ]
        print()
        if kept:
            print(f"  {ok(f'{len(kept)} task(s) merged')}")
        for turn in pending:
            assert turn.result
            print(f"  {warn('left on a branch')}  {turn.result.branch}  {dim(turn.task[:40])}")
        if pending:
            print(dim("\n  they are not merged. `forge code review` to look, "
                      "`forge code discard` to remove."))
        print()


def _base_branch(agent: CodingAgent, result: CodingResult) -> str:
    """The branch the run started from, or a sensible default."""
    del result
    for candidate in ("main", "master"):
        with contextlib.suppress(GitError):
            agent.repo.run("rev-parse", "--verify", candidate)
            return candidate
    return agent.repo.current_branch()


async def run_session(repo: Path = Path(), *, allow_dirty: bool = False) -> int:
    return await Session(repo=repo, allow_dirty=allow_dirty).loop()
