"""HTTP surface for the coding agent.

The agent has always been CLI-only. This is the same object driven over HTTP so
a browser can do what the terminal session does: browse the repository, give it
a task, watch the work, read the diff, and merge or discard it.

The safety model is unchanged and is not re-implemented here. Every task still
runs on its own branch, and `accept` is still a `git merge --no-ff` the user
asks for. A web button cannot merge anything the terminal could not, and
nothing merges without an explicit call - see `forge/coding/session.py`.

Mounted only when the server is started against a repository. A deployment
serving generic runs has no coding surface at all, which is the right default:
these endpoints read and write a working tree.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from forge.coding.agent import CodingAgent, CodingResult
from forge.coding.git import GitError, NotARepository
from forge.coding.memory import Conversation, Turn
from forge.config import ForgeConfig, ProviderConfig
from forge.ids import new_id

__all__ = ["CodingService", "build_coding_router"]

MAX_FILE_BYTES = 400_000
MAX_TREE_ENTRIES = 4_000


class SaveRequest(BaseModel):
    """Module scope, not inside the router factory.

    `from __future__ import annotations` makes every annotation a string, and
    FastAPI resolves those against the *module* namespace. A model declared
    inside the factory is invisible there, so FastAPI quietly demotes the
    parameter to a query field and every call 422s with what looks like a
    client error but is ours. `forge/api/app.py` documents the same trap for
    its dependency alias; I walked straight into it anyway.
    """

    path: str = Field(min_length=1)
    content: str


class ModelRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TaskRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=8_000)
    max_steps: int | None = Field(default=None, ge=1, le=60)


@dataclass
class Task:
    """One coding task and everything the UI needs to render it."""

    id: str
    goal: str
    status: str = "running"
    run_id: str | None = None
    branch: str | None = None
    commits: int = 0
    files: list[str] = field(default_factory=list)
    diff_stat: str = ""
    answer: str = ""
    """What the agent said it did. The point of the whole exchange, and it
    was being computed and discarded."""

    base_ref: str = ""
    stacked_on: str | None = None
    """The task this one was built on top of, when it started from that
    task's branch rather than from the base branch."""

    progress: list[dict[str, Any]] = field(default_factory=list)
    """Live trace of the run, for the app to render while it works."""

    error: str | None = None
    merged: bool = False
    discarded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "goal": self.goal, "status": self.status,
            "run_id": self.run_id, "branch": self.branch, "commits": self.commits,
            "files": self.files, "diff_stat": self.diff_stat, "error": self.error,
            "answer": self.answer,
            "base_ref": self.base_ref, "progress": self.progress,
            "stacked_on": self.stacked_on,
            "merged": self.merged, "discarded": self.discarded,
        }


def _default_config(repo: Path) -> ForgeConfig:
    """Configuration for a repository that has none.

    `ForgeConfig` defaults to the mock provider, which is right for the library
    and wrong here: opening Studio on a plain repository would show a model
    called `mock-1` and answer every task with canned text. A coding tool for
    local models should reach for the local model.

    So with no `forge.toml`, point at Ollama. Which model is a guess, and the
    picker exists precisely so a guess is cheap to correct - but a guess that
    can do the work beats a default that cannot.
    """
    config = ForgeConfig.load()
    if any(p.kind != "mock" for p in config.providers):
        return config
    del repo
    return replace(config, providers=(ProviderConfig(kind="ollama", model="qwen3:8b"),))


class CodingService:
    """Owns the agent, the task history, and the one-at-a-time rule.

    Concurrent tasks are refused rather than queued. Two agents branching from
    the same working tree at once would interleave commits, and a queue would
    only hide that behind a wait - the honest answer to "can I start another
    while this runs" is no.
    """

    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo).resolve()
        # One conversation per session, handed to every agent this service
        # builds - including the one rebuilt when the model changes, so
        # switching model mid-session does not amnesia the history.
        self.conversation = Conversation()
        self.agent = CodingAgent(
            self.repo,
            config=_default_config(Path(repo)),
            conversation=self.conversation,
        )
        self.agent.repo.require_repo()
        # The runtime's own state lives in the repository, so without this it
        # shows up as the user's uncommitted change the first time Studio is
        # asked what they have modified. A task start does this too, but Studio
        # can be asked before any task has run.
        with contextlib.suppress(GitError):
            self.agent.repo.exclude_runtime_state()
        self.tasks: dict[str, Task] = {}
        self.order: list[str] = []
        self._running: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        return self._running is not None and not self._running.done()

    # -- repository ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        repo = self.agent.repo
        model = "not configured"
        with contextlib.suppress(Exception):
            providers = self.agent.config.providers
            if providers:
                model = f"{providers[0].kind}/{providers[0].model}"
        # A repository with no commits has no HEAD, so `rev-parse HEAD` fails
        # with 128 and the whole status call used to raise - leaving the header
        # blank on exactly the repository that most needs an explanation.
        # `branch --show-current` answers on an unborn branch, and the missing
        # commit is reported as a fact rather than an error.
        branch, has_commits = "", False
        with contextlib.suppress(GitError):
            has_commits = self.agent.repo.has_commits()
        with contextlib.suppress(GitError):
            branch = (
                repo.current_branch() if has_commits
                else repo.run("branch", "--show-current").strip()
            )

        return {
            "root": str(self.repo),
            "name": self.repo.name,
            "branch": branch or "(no branch yet)",
            "has_commits": has_commits,
            "clean": repo.is_clean(),
            "dirty_files": repo.dirty_files()[:50],
            "model": model,
            "policy": self.agent._policy.version,
            "busy": self.busy,
        }

    async def models(self) -> dict[str, Any]:
        """Which models this machine can actually run, and which is selected.

        Asked of the Ollama daemon rather than read from configuration: the
        point of the list is to show what is installed, and a config file
        happily names a model that was never pulled. Each entry says whether it
        is usable, so the picker can show a model that needs `ollama pull`
        rather than silently omitting it.
        """
        from forge.llm.ollama import OllamaProvider

        active = ""
        providers = self.agent.config.providers
        if providers:
            active = providers[0].model

        probe = OllamaProvider(model=active or "qwen3:8b")
        installed: list[dict[str, Any]] = []
        reachable = True
        try:
            client = await probe._http()
            resp = await client.get("/api/tags", timeout=4.0)
            resp.raise_for_status()
            for entry in resp.json().get("models", []):
                details = entry.get("details") or {}
                installed.append(
                    {
                        "name": str(entry.get("name", "")),
                        "size_gb": round(float(entry.get("size", 0)) / 1e9, 1),
                        "parameters": details.get("parameter_size") or "",
                        "family": details.get("family") or "",
                    }
                )
        except Exception:
            reachable = False
        finally:
            await probe.aclose()

        # Embedding models cannot drive an agent loop; offering them as a
        # choice would only produce a confusing failure later.
        chat = [m for m in installed if "embed" not in m["name"].lower()]
        chat.sort(key=lambda m: m["name"])
        return {
            "active": active,
            "reachable": reachable,
            "installed": chat,
            "host": probe.host,
        }

    def use_model(self, name: str) -> dict[str, Any]:
        """Switch the agent to another local model, for this session.

        The agent is rebuilt rather than mutated: it caches a policy bundle and
        a workspace, and reaching in to change one field of a frozen config
        would leave those describing a model that is no longer in use.
        `forge.toml` is deliberately not written - a picker is a thing you try,
        and silently editing a project's configuration because someone looked
        at a dropdown is not a trade worth making.
        """
        if self.busy:
            raise HTTPException(
                status_code=409, detail="a task is running; wait for it to finish"
            )
        if not name.strip():
            raise HTTPException(status_code=400, detail="no model named")

        config = self.agent.config
        providers = config.providers or (ProviderConfig(kind="ollama"),)
        head = replace(providers[0], kind="ollama", model=name)
        self.agent = CodingAgent(
            self.repo,
            config=replace(config, providers=(head, *providers[1:])),
            conversation=self.conversation,
        )
        self.agent.repo.require_repo()
        return {"active": name}

    def tree(self) -> list[str]:
        """Every file in the repository that git is not ignoring.

        `--cached --others --exclude-standard` is tracked *plus* untracked, and
        the distinction matters more than it looks: plain `ls-files` lists only
        tracked files, so a project whose first commit has not happened yet
        shows an empty explorer. One real repository here had 194 files and
        displayed none of them.

        Still asked of git rather than walked, so `.gitignore` is honoured for
        free and the tree cannot fill with `node_modules` or `.venv` - and what
        the explorer shows stays exactly what the agent would consider part of
        the repository.
        """
        out = self.agent.repo.run(
            "ls-files", "--cached", "--others", "--exclude-standard"
        )
        seen = {line.strip() for line in out.splitlines() if line.strip()}
        return sorted(seen)[:MAX_TREE_ENTRIES]

    def _safe(self, relative: str) -> Path:
        """Resolve inside the repository, or refuse.

        `../` in a path parameter is the oldest trick there is, and this
        endpoint reads files.
        """
        target = (self.repo / relative).resolve()
        if not target.is_relative_to(self.repo):
            raise HTTPException(status_code=400, detail="path escapes the repository")
        return target

    def read_file(self, relative: str) -> dict[str, Any]:
        target = self._safe(relative)
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"no such file: {relative}")
        raw = target.read_bytes()
        if len(raw) > MAX_FILE_BYTES:
            return {"path": relative, "truncated": True, "content":
                    raw[:MAX_FILE_BYTES].decode("utf-8", "replace")}
        try:
            return {"path": relative, "truncated": False, "content": raw.decode("utf-8")}
        except UnicodeDecodeError:
            return {"path": relative, "binary": True, "content": ""}

    def write_file(self, relative: str, content: str) -> dict[str, Any]:
        """Save an edit from the editor.

        The agent is not the only one who may change this repository - the
        person watching it is entitled to fix a line themselves. Writes land
        in the working tree exactly as an editor's would, so git sees them as
        ordinary uncommitted changes and nothing about the branch model
        changes.
        """
        target = self._safe(relative)
        if not target.parent.exists():
            raise HTTPException(status_code=400, detail="directory does not exist")
        target.write_text(content, encoding="utf-8", newline="")
        return {"path": relative, "bytes": len(content.encode("utf-8"))}

    def search(self, query: str, limit: int = 200) -> list[dict[str, Any]]:
        """Grep the tracked files, via git so ignored paths stay ignored."""
        if not query.strip():
            return []
        try:
            raw = self.agent.repo.run("grep", "-n", "-I", "--fixed-strings", query)
        except GitError:
            return []  # git grep exits non-zero when there are no matches
        hits: list[dict[str, Any]] = []
        for line in raw.splitlines()[:limit]:
            path, _, rest = line.partition(":")
            number, _, text = rest.partition(":")
            if number.isdigit():
                hits.append({"path": path, "line": int(number), "text": text[:220]})
        return hits

    # -- tasks -----------------------------------------------------------

    def start(self, goal: str, max_steps: int | None) -> Task:
        if self.busy:
            raise HTTPException(
                status_code=409,
                detail="a task is already running; wait for it to finish",
            )
        task = Task(id=new_id("task"), goal=goal)
        # A run branches from wherever HEAD is. After a previous task that is
        # the previous task's branch, so this one stacks on it - which is what
        # makes iterating work, and what the panel has to say out loud.
        with contextlib.suppress(GitError):
            head = self.agent.repo.current_branch()
            task.stacked_on = next(
                (
                    other.id
                    for other in reversed([self.tasks[i] for i in self.order])
                    if other.branch == head and not other.merged and not other.discarded
                ),
                None,
            )
        self.tasks[task.id] = task
        self.order.append(task.id)
        self._running = asyncio.create_task(self._execute(task, max_steps))
        return task

    def _note(self, task: Task, kind: str, text: str) -> None:
        task.progress.append({"kind": kind, "text": text})
        del task.progress[:-200]  # a long run should not grow without bound

    def _on_step(self, task: Task) -> Any:
        """Translate runtime events into lines worth watching.

        Only what answers "is it working, and on what". The full log is
        already available through the run console; repeating it here would
        make the pane unreadable at exactly the moment it matters.
        """
        def hook(event_type: Any, payload: dict[str, Any]) -> None:
            name = getattr(event_type, "value", str(event_type))
            tool = payload.get("tool")
            if name == "STEP_COMMITTED":
                step = payload.get("index", "")
                if payload.get("ok"):
                    self._note(task, "ok", f"step {step} ran {tool or 'a tool'}".strip())
                else:
                    detail = str(payload.get("detail", ""))[:110]
                    self._note(
                        task, "bad",
                        f"step {step} {tool or 'a tool'} failed: {detail}".strip(),
                    )
            elif name == "PROPOSAL_RECEIVED":
                # The rationale is the model explaining itself, which is most of
                # what makes a coding agent readable rather than a progress bar.
                why = str(payload.get("rationale_summary") or "").strip()
                if tool:
                    self._note(task, "plan", f"{tool}" + (f" - {why}" if why else ""))
                elif why:
                    self._note(task, "think", why)
            elif name == "EFFECT_OBSERVED" and tool:
                self._note(task, "ok" if payload.get("ok") else "bad",
                           f"{'ran' if payload.get('ok') else 'failed'} {tool}")
            elif name == "EFFECT_REUSED" and tool:
                self._note(task, "warn", f"skipped {tool} - already done")
            elif name == "POLICY_DECIDED" and payload.get("decision") == "DENY":
                self._note(task, "warn", f"refused {payload.get('capability', '')}")
            elif name == "LOOP_DETECTED":
                self._note(task, "warn", "looping - " + str(payload.get("action", "")))
        return hook

    async def _execute(self, task: Task, max_steps: int | None) -> None:
        try:
            result: CodingResult = await self.agent.run(
                task.goal, max_steps=max_steps, on_step=self._on_step(task)
            )
            task.run_id = result.run.run_id
            task.branch = result.branch
            task.commits = result.commits
            task.files = list(result.files_touched)
            task.diff_stat = result.diff_stat
            task.base_ref = result.base_ref
            task.answer = result.run.answer or ""
            task.status = "completed" if result.ok else "failed"
            if not result.ok:
                task.error = result.run.error
            # Recorded whatever the outcome: a task that failed is exactly the
            # context a follow-up needs, and dropping it is how "try that again
            # differently" ends up repeating the same thing.
            self.conversation.record(
                Turn(
                    goal=task.goal,
                    status=task.status,
                    answer=result.run.answer or "",
                    files=tuple(task.files),
                    commits=task.commits,
                    branch=task.branch or "",
                )
            )
        except (NotARepository, GitError) as exc:
            task.status, task.error = "failed", str(exc)
        except asyncio.CancelledError:
            # Stopped on purpose. Whatever the agent committed before the stop
            # is still on its branch, which is the point of the branch - so
            # this is a state you can read and discard, not work that vanished.
            task.status = "cancelled"
            task.error = "stopped before it finished"
            self.conversation.record(
                Turn(goal=task.goal, status="cancelled", branch=task.branch or "")
            )
            self._note(task, "warn", "stopped")
            raise
        except Exception as exc:
            task.status, task.error = "failed", f"{type(exc).__name__}: {exc}"

    def cancel(self, task_id: str) -> dict[str, Any]:
        """Stop a running task.

        Only one task runs at a time, deliberately - two agents branching from
        one working tree would interleave commits. That makes an unstoppable
        task a blocked application, so stopping has to be possible. Partial
        work stays on the branch and is discarded the same way any other task
        is.
        """
        task = self.get(task_id)
        if task.status != "running" or self._running is None:
            raise HTTPException(status_code=409, detail="that task is not running")
        self._running.cancel()
        return {"cancelled": task.id}

    def get(self, task_id: str) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"no such task: {task_id}")
        return task

    def _base_branch(self) -> str:
        """The branch a task should merge back into.

        Same rule the terminal session uses. Not `current_branch()`: a run
        leaves HEAD on the agent's own branch, so asking git where we are
        after a task returns the task branch itself.
        """
        for candidate in ("main", "master"):
            with contextlib.suppress(GitError):
                self.agent.repo.run("rev-parse", "--verify", candidate)
                return candidate
        return self.agent.repo.current_branch()

    def diff(self, task_id: str) -> str:
        """What this task changed, measured from where it started.

        Diffing against `current_branch()` silently returns nothing, because
        after a run that *is* the task branch - a branch compared with itself.
        The base commit is recorded when the run starts precisely so this
        question has an answer that does not depend on where HEAD wandered.
        """
        task = self.get(task_id)
        if not task.branch:
            return ""
        base = task.base_ref or self._base_branch()
        with contextlib.suppress(GitError):
            return self.agent.repo.run("diff", f"{base}...{task.branch}")
        return ""

    def stacked_above(self, task_id: str) -> list[str]:
        """Live tasks built on top of this one, newest last."""
        return [
            self.tasks[i].id
            for i in self.order
            if self.tasks[i].stacked_on == task_id
            and not self.tasks[i].discarded
            and not self.tasks[i].merged
        ]

    def working_changes(self) -> dict[str, Any]:
        """What *you* have changed and not committed.

        Studio lets you edit a file, so it has to let you read that edit. An
        editor built around reviewing diffs that will not show your own is
        incoherent - and the uncommitted state is also what the agent will
        branch from, so it is worth seeing before starting a task.
        """
        diff = ""
        files: list[str] = []
        with contextlib.suppress(GitError):
            diff = self.agent.repo.run("diff")
            files = [
                line[3:].strip()
                for line in self.agent.repo.run("status", "--porcelain").splitlines()
                if line.strip()
            ]
        return {"diff": diff, "files": files, "clean": not files}

    def file_diff(self, task_id: str, path: str) -> str:
        """One file's diff. The whole-task diff is unreadable past a few files,
        and reading the change is the step this product exists to make easy."""
        task = self.get(task_id)
        if not task.branch:
            return ""
        base = task.base_ref or self._base_branch()
        with contextlib.suppress(GitError):
            return self.agent.repo.run("diff", f"{base}...{task.branch}", "--", path)
        return ""

    async def audit(self, task_id: str) -> list[dict[str, Any]]:
        """The durable event log for this task's run.

        Every claim this project makes - authorized, recorded, recoverable -
        is a claim about this log. It was reachable through the run console and
        `forge trace`, but not from the place where the work is actually being
        judged, which is the one place it matters most.
        """
        task = self.get(task_id)
        if not task.run_id:
            return []
        from forge.config import ForgeConfig
        from forge.state.sqlite_store import SQLiteEventStore

        store = SQLiteEventStore(ForgeConfig.load().sqlite_path)
        await store.open()
        try:
            events = await store.read(task.run_id)
        finally:
            await store.close()
        return [
            {
                "seq": e.seq,
                "step": e.step_index,
                "type": e.type.value,
                "payload": {
                    k: v
                    for k, v in e.payload.items()
                    if k in ("tool", "decision", "reason", "capability", "ok",
                             "detail", "error", "phase", "action", "model")
                },
            }
            for e in events
        ]

    def accept(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if not task.branch or task.commits == 0:
            raise HTTPException(status_code=400, detail="this task changed nothing to merge")
        if task.discarded:
            raise HTTPException(status_code=400, detail="this task was already discarded")
        later = self.stacked_above(task_id)
        if later:
            raise HTTPException(
                status_code=409,
                detail=(
                    "a later task was built on this one; merging this alone would "
                    "leave that work stranded. Merge the newest task instead - it "
                    "already contains this one."
                ),
            )
        try:
            # A run leaves HEAD on the agent's branch. Merging from there
            # merges a branch into itself: git reports success, the base
            # branch gains nothing, and the UI says "merged" while the work
            # is still stranded. Go back first, exactly as the terminal
            # session does.
            if self.agent.repo.current_branch() == task.branch:
                self.agent.repo.checkout(self._base_branch())
            self.agent.repo.run(
                "merge", "--no-ff", "-m", f"forge: {task.goal[:70]}", task.branch
            )
        except GitError as exc:
            raise HTTPException(status_code=409, detail=f"merge failed: {exc}") from exc
        task.merged = True
        return {"merged": task.branch, "into": self.agent.repo.current_branch()}

    def undo(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if task.merged:
            raise HTTPException(
                status_code=400,
                detail="already merged; use git revert rather than deleting the branch",
            )
        if task.branch:
            with contextlib.suppress(GitError):
                # Cannot delete the branch you are standing on.
                if self.agent.repo.current_branch() == task.branch:
                    self.agent.repo.checkout(self._base_branch())
                self.agent.repo.delete_branch(task.branch)
        task.discarded = True
        return {"discarded": task.branch}


def build_coding_router(
    service: CodingService, *, dependencies: Sequence[Any] = ()
) -> APIRouter:
    """Build the router, guarded by whatever the app authenticates with.

    `dependencies` is not optional in practice and the caller supplies the
    app's own authenticator. These endpoints read and write a working tree and
    start an agent against it; leaving them open because the server binds to
    loopback assumes nothing else on the machine is hostile, and a browser
    visiting the wrong page is enough to break that assumption.
    """
    router = APIRouter(prefix="/code", tags=["coding"], dependencies=list(dependencies))

    @router.get("/status")
    async def status() -> dict[str, Any]:
        return service.status()

    @router.get("/models")
    async def models() -> dict[str, Any]:
        return await service.models()

    @router.post("/models")
    async def use_model(body: ModelRequest) -> dict[str, Any]:
        return service.use_model(body.name)

    @router.get("/tree")
    async def tree() -> dict[str, Any]:
        return {"files": service.tree()}

    @router.get("/file")
    async def file(path: str = Query(min_length=1)) -> dict[str, Any]:
        return service.read_file(path)

    @router.put("/file")
    async def save_file(body: SaveRequest) -> dict[str, Any]:
        return service.write_file(body.path, body.content)

    @router.get("/search")
    async def search(q: str = Query(min_length=1, max_length=200)) -> dict[str, Any]:
        return {"hits": service.search(q)}

    @router.get("/tasks")
    async def list_tasks() -> list[dict[str, Any]]:
        out = []
        for tid in reversed(service.order):
            row = service.tasks[tid].to_dict()
            row["stacked_above"] = service.stacked_above(tid)
            out.append(row)
        return out

    @router.post("/tasks", status_code=202)
    async def start_task(body: TaskRequest) -> dict[str, Any]:
        return service.start(body.goal, body.max_steps).to_dict()

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        return service.get(task_id).to_dict()

    @router.get("/tasks/{task_id}/diff")
    async def task_diff(task_id: str) -> dict[str, Any]:
        return {"diff": service.diff(task_id)}

    @router.get("/tasks/{task_id}/diff/file")
    async def task_file_diff(task_id: str, path: str = Query(min_length=1)) -> dict[str, Any]:
        return {"diff": service.file_diff(task_id, path)}

    @router.post("/tasks/{task_id}/cancel")
    async def cancel(task_id: str) -> dict[str, Any]:
        return service.cancel(task_id)

    @router.get("/tasks/{task_id}/audit")
    async def audit(task_id: str) -> list[dict[str, Any]]:
        return await service.audit(task_id)

    @router.get("/changes")
    async def working_changes() -> dict[str, Any]:
        return service.working_changes()

    @router.post("/tasks/{task_id}/accept")
    async def accept(task_id: str) -> dict[str, Any]:
        return service.accept(task_id)

    @router.post("/tasks/{task_id}/undo")
    async def undo(task_id: str) -> dict[str, Any]:
        return service.undo(task_id)

    return router
