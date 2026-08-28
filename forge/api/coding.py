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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from forge.coding.agent import CodingAgent, CodingResult
from forge.coding.git import GitError, NotARepository
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
    base_ref: str = ""
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
            "base_ref": self.base_ref, "progress": self.progress,
            "merged": self.merged, "discarded": self.discarded,
        }


class CodingService:
    """Owns the agent, the task history, and the one-at-a-time rule.

    Concurrent tasks are refused rather than queued. Two agents branching from
    the same working tree at once would interleave commits, and a queue would
    only hide that behind a wait - the honest answer to "can I start another
    while this runs" is no.
    """

    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo).resolve()
        self.agent = CodingAgent(self.repo)
        self.agent.repo.require_repo()
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
        return {
            "root": str(self.repo),
            "name": self.repo.name,
            "branch": repo.current_branch(),
            "clean": repo.is_clean(),
            "dirty_files": repo.dirty_files()[:50],
            "model": model,
            "policy": self.agent._policy.version,
            "busy": self.busy,
        }

    def tree(self) -> list[str]:
        """Tracked files, from git rather than a directory walk.

        `git ls-files` already honours .gitignore, so the tree cannot fill up
        with `node_modules` or `.venv` - and what the UI shows is exactly what
        the agent would consider part of the repository.
        """
        out = self.agent.repo.run("ls-files")
        return sorted(line.strip() for line in out.splitlines() if line.strip())[
            :MAX_TREE_ENTRIES
        ]

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
                self._note(task, "ok", f"step {payload.get('index', '')} "
                                       f"ran {tool or 'a tool'}".strip())
            elif name == "PROPOSAL_RECEIVED" and tool:
                self._note(task, "plan", f"proposes {tool}")
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
            task.status = "completed" if result.ok else "failed"
            if not result.ok:
                task.error = result.run.error
        except (NotARepository, GitError) as exc:
            task.status, task.error = "failed", str(exc)
        except Exception as exc:
            task.status, task.error = "failed", f"{type(exc).__name__}: {exc}"

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

    def accept(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if not task.branch or task.commits == 0:
            raise HTTPException(status_code=400, detail="this task changed nothing to merge")
        if task.discarded:
            raise HTTPException(status_code=400, detail="this task was already discarded")
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


def build_coding_router(service: CodingService) -> APIRouter:
    router = APIRouter(prefix="/code", tags=["coding"])

    @router.get("/status")
    async def status() -> dict[str, Any]:
        return service.status()

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
        return [service.tasks[t].to_dict() for t in reversed(service.order)]

    @router.post("/tasks", status_code=202)
    async def start_task(body: TaskRequest) -> dict[str, Any]:
        return service.start(body.goal, body.max_steps).to_dict()

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        return service.get(task_id).to_dict()

    @router.get("/tasks/{task_id}/diff")
    async def task_diff(task_id: str) -> dict[str, Any]:
        return {"diff": service.diff(task_id)}

    @router.post("/tasks/{task_id}/accept")
    async def accept(task_id: str) -> dict[str, Any]:
        return service.accept(task_id)

    @router.post("/tasks/{task_id}/undo")
    async def undo(task_id: str) -> dict[str, Any]:
        return service.undo(task_id)

    return router
