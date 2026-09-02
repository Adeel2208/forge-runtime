"""The coding agent's HTTP surface.

Two of these are regressions against bugs I introduced by re-implementing
logic the terminal session already had, which is exactly what the module
docstring says not to do. Both were the same shape: the code looked right and
git reported success, while the user's branch gained nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException

from forge.api.coding import CodingService, Task


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    src = root / "src"
    src.mkdir()
    (src / "calc.py").write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True,
                       capture_output=True, text=True)

    git("init", "-q", "-b", "master")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "initial")
    return root


def _branch_with_change(service: CodingService, name: str) -> Task:
    """Simulate a finished run: a branch with a commit, HEAD left on it.

    The agent leaves HEAD on its own branch, and that detail is what both
    bugs below turned on, so the fixture reproduces it rather than tidying
    it away.
    """
    repo = service.agent.repo
    base = repo.head()
    repo.create_branch(name)
    target = service.repo / "src" / "calc.py"
    target.write_text(
        "def divide(a, b):\n    if b == 0:\n        raise ValueError('nope')\n    return a / b\n",
        encoding="utf-8",
    )
    repo.commit_all("agent edit")
    task = Task(id="task_x", goal="guard divide", status="completed",
                branch=name, commits=1, base_ref=base, files=["src/calc.py"])
    service.tasks[task.id] = task
    service.order.append(task.id)
    return task



def _start(service: CodingService, goal: str) -> Task:
    """Start a task, then cancel the run immediately.

    `start()` is what computes the stack, and it needs a running loop to
    schedule the work. The work itself needs a model, which these tests
    deliberately do not have - so the run is cancelled the moment it is
    scheduled and only the bookkeeping is exercised.
    """
    import asyncio
    import contextlib as ctx

    async def go() -> Task:
        task = service.start(goal, None)
        if service._running is not None:
            service._running.cancel()
            with ctx.suppress(BaseException):
                await service._running
        return task

    return asyncio.run(go())


# -- the diff --------------------------------------------------------------


def test_the_diff_is_measured_from_where_the_task_started(tmp_path) -> None:
    """Diffing against `current_branch()` returns nothing.

    After a run, HEAD *is* the task branch, so that comparison is a branch
    against itself: the endpoint returned an empty string and the UI said
    "This task changed nothing" about a task that had just changed something.
    """
    service = CodingService(_repo(tmp_path))
    task = _branch_with_change(service, "forge/code_1")

    assert service.agent.repo.current_branch() == "forge/code_1", "fixture precondition"

    diff = service.diff(task.id)
    assert diff.strip(), "the diff must not be empty for a task that changed a file"
    assert "raise ValueError" in diff
    assert "src/calc.py" in diff


# -- the merge -------------------------------------------------------------


def test_accept_merges_into_the_base_branch_not_the_task_branch(tmp_path) -> None:
    """The worse of the two bugs: git reported success and nothing moved.

    Merging while HEAD is on the agent's branch merges that branch into
    itself. Git says "Already up to date", the endpoint returned
    {"merged": ...}, and the user's branch never received the work - a UI
    that confidently reports a merge that did not happen.
    """
    service = CodingService(_repo(tmp_path))
    task = _branch_with_change(service, "forge/code_2")

    before = service.agent.repo.run("show", "master:src/calc.py")
    assert "ValueError" not in before, "fixture precondition"

    result = service.accept(task.id)

    after = service.agent.repo.run("show", "master:src/calc.py")
    assert "ValueError" in after, "master did not actually receive the change"
    assert result["into"] == "master"
    assert service.agent.repo.current_branch() == "master"
    assert service.tasks[task.id].merged is True


def test_undo_deletes_the_branch_and_leaves_the_base_untouched(tmp_path) -> None:
    service = CodingService(_repo(tmp_path))
    task = _branch_with_change(service, "forge/code_3")

    service.undo(task.id)

    assert "ValueError" not in service.agent.repo.run("show", "master:src/calc.py")
    assert "forge/code_3" not in service.agent.repo.run("branch")
    assert service.tasks[task.id].discarded is True


def test_a_merged_task_cannot_be_undone(tmp_path) -> None:
    """Deleting the branch would not remove the commits; `git revert` would."""
    service = CodingService(_repo(tmp_path))
    task = _branch_with_change(service, "forge/code_4")
    service.accept(task.id)

    with pytest.raises(HTTPException) as caught:
        service.undo(task.id)
    assert caught.value.status_code == 400
    assert "revert" in str(caught.value.detail)


def test_a_task_that_changed_nothing_cannot_be_merged(tmp_path) -> None:
    service = CodingService(_repo(tmp_path))
    empty = Task(id="task_empty", goal="nothing", status="completed", commits=0)
    service.tasks[empty.id] = empty

    with pytest.raises(HTTPException) as caught:
        service.accept(empty.id)
    assert caught.value.status_code == 400


# -- reading the repository ------------------------------------------------


def test_a_path_cannot_escape_the_repository(tmp_path) -> None:
    """This endpoint reads files off disk; `../` is the oldest trick there is."""
    service = CodingService(_repo(tmp_path))
    for attempt in ("../secret.txt", "../../etc/passwd", "src/../../out.txt"):
        with pytest.raises(HTTPException) as caught:
            service.read_file(attempt)
        assert caught.value.status_code == 400


def test_the_tree_comes_from_git_not_a_directory_walk(tmp_path) -> None:
    """`git ls-files` honours .gitignore, so the tree cannot fill with junk,
    and what is shown is what the agent considers part of the repository."""
    root = _repo(tmp_path)
    (root / ".gitignore").write_text("junk/\n", encoding="utf-8")
    (root / "junk").mkdir()
    (root / "junk" / "big.bin").write_text("x", encoding="utf-8")

    service = CodingService(root)
    files = service.tree()
    assert "src/calc.py" in files
    assert not any(f.startswith("junk/") for f in files)


def test_reading_a_missing_file_is_a_404(tmp_path) -> None:
    service = CodingService(_repo(tmp_path))
    with pytest.raises(HTTPException) as caught:
        service.read_file("src/nope.py")
    assert caught.value.status_code == 404


def test_status_reports_what_the_workbench_header_shows(tmp_path) -> None:
    service = CodingService(_repo(tmp_path))
    status = service.status()
    assert status["branch"] == "master"
    assert status["name"] == "repo"
    assert status["busy"] is False
    assert "policy" in status


def test_a_subdirectory_is_refused(tmp_path) -> None:
    """Same guard the terminal session applies: the agent commits whole
    repositories, so running from a subdirectory would sweep in unrelated
    work."""
    root = _repo(tmp_path)
    nested = root / "src"
    from forge.coding.git import NotARepository

    with pytest.raises(NotARepository):
        CodingService(nested)


# -- editing and searching -------------------------------------------------


def test_the_editor_can_save_a_file(tmp_path) -> None:
    """The agent is not the only one allowed to change this repository.

    Writes land in the working tree exactly as an editor's would, so git sees
    ordinary uncommitted changes and the branch model is untouched.
    """
    service = CodingService(_repo(tmp_path))
    service.write_file("src/calc.py", "def divide(a, b):\n    return b\n")

    assert (service.repo / "src" / "calc.py").read_text(encoding="utf-8").endswith("return b\n")
    assert "src/calc.py" in service.agent.repo.dirty_files()


def test_a_save_cannot_escape_the_repository(tmp_path) -> None:
    service = CodingService(_repo(tmp_path))
    with pytest.raises(HTTPException) as caught:
        service.write_file("../../evil.txt", "x")
    assert caught.value.status_code == 400


def test_search_finds_text_and_reports_line_numbers(tmp_path) -> None:
    service = CodingService(_repo(tmp_path))
    hits = service.search("divide")
    assert hits and hits[0]["path"] == "src/calc.py"
    assert hits[0]["line"] == 1
    assert "divide" in hits[0]["text"]


def test_search_with_no_matches_is_empty_not_an_error(tmp_path) -> None:
    """`git grep` exits non-zero when nothing matches; that is not a failure."""
    service = CodingService(_repo(tmp_path))
    assert service.search("nothing_matches_this_xyz") == []


def test_the_save_model_is_declared_at_module_scope() -> None:
    """`from __future__ import annotations` makes annotations strings, and
    FastAPI resolves them against the module namespace. A model declared
    inside the router factory is invisible there, so every save 422s with
    what looks like a client error. This repository documents the same trap
    in `api/app.py`; the check keeps it fixed."""
    import forge.api.coding as mod

    assert hasattr(mod, "SaveRequest"), "SaveRequest must be importable from the module"


# -- authorization ---------------------------------------------------------


def test_every_coding_endpoint_requires_a_key(tmp_path) -> None:
    """These endpoints read and write a working tree and start an agent
    against it. Leaving them open because the server binds to loopback assumes
    nothing else on the machine is hostile, and a browser visiting the wrong
    page is enough to break that assumption.

    This shipped open for one commit; the test is what keeps it shut.
    """
    import subprocess

    from fastapi.testclient import TestClient

    from forge.api.app import create_app
    from forge.config import BudgetConfig, ForgeConfig, ProviderConfig
    from forge.deployment import Forge
    from forge.llm.mock import MockProvider

    root = _repo(tmp_path)
    subprocess.run(["git", "-C", str(root), "status"], check=True, capture_output=True)

    config = ForgeConfig(
        database_url=f"sqlite:///{tmp_path / 'auth.db'}",
        providers=(ProviderConfig(kind="mock"),),
        budget=BudgetConfig(max_steps=4),
    )
    keys = {"d1a2b0f8a7fbbcb2b3cbeb0ae44e0d6a41ff8b2e0e9d0f4b1f0e7d0a9c1b2d3e": "t"}
    app = create_app(
        config, deployment=Forge(config=config, providers=[MockProvider([])]),
        configure_logs=False, require_auth=True, api_keys=keys, repo=str(root),
    )

    with TestClient(app) as client:
        for method, path, body in (
            ("get", "/code/status", None),
            ("get", "/code/tree", None),
            ("get", "/code/file?path=src/calc.py", None),
            ("get", "/code/search?q=divide", None),
            ("get", "/code/tasks", None),
            ("put", "/code/file", {"path": "src/calc.py", "content": "x"}),
            ("post", "/code/tasks", {"goal": "do a thing"}),
        ):
            resp = getattr(client, method)(path, **({"json": body} if body else {}))
            assert resp.status_code == 401, f"{method.upper()} {path} is not guarded"

        # The page itself stays open: it carries no repository data.
        assert client.get("/code").status_code == 200


# -- stacked tasks ---------------------------------------------------------


def test_a_task_started_from_a_previous_branch_records_the_stack(tmp_path) -> None:
    """A run branches from wherever HEAD is, and a finished run leaves HEAD on
    its own branch - so iterating stacks tasks. That is the right behaviour;
    it just has to be visible."""
    service = CodingService(_repo(tmp_path))
    first = _branch_with_change(service, "forge/code_a")

    # HEAD is on the first task's branch, exactly as a finished run leaves it.
    assert service.agent.repo.current_branch() == "forge/code_a"
    second = _start(service, "follow-up")

    assert second.stacked_on == first.id
    assert service.stacked_above(first.id) == [second.id]


def test_merging_an_earlier_task_alone_is_refused(tmp_path) -> None:
    """Merging it would leave the later task's work stranded on a branch
    whose base has moved, and merging the newest already includes this one."""
    service = CodingService(_repo(tmp_path))
    first = _branch_with_change(service, "forge/code_b")
    second = _start(service, "follow-up")
    second.status, second.commits, second.branch = "completed", 1, "forge/code_c"

    with pytest.raises(HTTPException) as caught:
        service.accept(first.id)
    assert caught.value.status_code == 409
    assert "newest" in str(caught.value.detail)


def test_a_discarded_follow_up_stops_blocking_the_earlier_task(tmp_path) -> None:
    service = CodingService(_repo(tmp_path))
    first = _branch_with_change(service, "forge/code_d")
    second = _start(service, "follow-up")
    second.status, second.commits, second.branch = "completed", 1, "forge/code_e"

    service.undo(second.id)
    assert service.stacked_above(first.id) == []
    assert service.accept(first.id)["into"] == "master"


def test_a_repository_with_no_config_reaches_for_a_local_model(tmp_path) -> None:
    """`ForgeConfig` defaults to the mock provider, which is right for the
    library and wrong for Studio: opening a plain repository would show a model
    called `mock-1` and answer every task with canned text. The README's
    documented path is `cd your-project && forge studio`, with no `forge init`
    in it, so that path has to reach for the local model."""
    service = CodingService(_repo(tmp_path))
    kinds = {p.kind for p in service.agent.config.providers}

    assert "mock" not in kinds, "a coding agent must not default to a mock provider"
    assert service.status()["model"].startswith("ollama/")


def test_an_explicit_configuration_is_never_overridden(tmp_path, monkeypatch) -> None:
    """The default only applies where there is nothing to respect."""
    from forge.api import coding as mod
    from forge.config import BudgetConfig, ForgeConfig, ProviderConfig

    chosen = ForgeConfig(
        providers=(ProviderConfig(kind="openai", model="gpt-4o-mini"),),
        budget=BudgetConfig(),
    )
    monkeypatch.setattr(mod.ForgeConfig, "load", staticmethod(lambda *a, **k: chosen))

    service = CodingService(_repo(tmp_path))
    assert service.agent.config.providers[0].kind == "openai"


# -- stop, review your own work, read the trail ----------------------------


def test_your_own_uncommitted_changes_are_readable(tmp_path) -> None:
    """Studio lets you edit a file, so it has to let you read that edit. An
    editor built around reviewing diffs that will not show your own is
    incoherent."""
    service = CodingService(_repo(tmp_path))
    assert service.working_changes()["clean"] is True

    (service.repo / "src" / "calc.py").write_text("changed\n", encoding="utf-8")
    changes = service.working_changes()

    assert changes["clean"] is False
    assert "src/calc.py" in changes["files"]
    assert "changed" in changes["diff"]


def test_the_runtime_state_is_not_reported_as_your_change(tmp_path) -> None:
    """`.forge/` lives in the repository. Reporting it as something the user
    modified is noise in the one view that must be trustworthy."""
    service = CodingService(_repo(tmp_path))
    (service.repo / ".forge").mkdir(exist_ok=True)
    (service.repo / ".forge" / "forge.db").write_bytes(b"x")

    assert service.working_changes()["clean"] is True


def test_cancelling_a_task_that_is_not_running_is_refused(tmp_path) -> None:
    service = CodingService(_repo(tmp_path))
    task = _branch_with_change(service, "forge/code_z")

    with pytest.raises(HTTPException) as caught:
        service.cancel(task.id)
    assert caught.value.status_code == 409


def test_a_task_with_no_run_has_an_empty_trail(tmp_path) -> None:
    """A task that never reached the runtime has nothing to show, and must say
    so rather than raising."""
    import asyncio

    service = CodingService(_repo(tmp_path))
    task = _branch_with_change(service, "forge/code_y")
    assert asyncio.run(service.audit(task.id)) == []


# -- the explorer ----------------------------------------------------------


def test_untracked_files_appear_in_the_tree(tmp_path) -> None:
    """Plain `git ls-files` lists tracked files only, so a project whose first
    commit has not happened shows an empty explorer. One real repository had
    194 files and displayed none of them."""
    root = _repo(tmp_path)
    (root / "brand_new.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "also_new.py").write_text("y = 2\n", encoding="utf-8")

    files = CodingService(root).tree()

    assert "brand_new.py" in files, "an uncommitted file is still a file"
    assert "src/also_new.py" in files
    assert "src/calc.py" in files, "tracked files are still listed"


def test_a_repository_with_nothing_committed_still_lists_its_files(tmp_path) -> None:
    import subprocess

    root = tmp_path / "fresh"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    (root / "notes.md").write_text("# notes\n", encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t.t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    files = CodingService(root).tree()
    assert sorted(files) == ["notes.md", "src/app.py"]


def test_ignored_files_stay_out_of_the_tree(tmp_path) -> None:
    """Listing untracked files must not mean listing build output."""
    root = _repo(tmp_path)
    (root / ".gitignore").write_text("junk/\n*.pyc\n", encoding="utf-8")
    (root / "junk").mkdir()
    (root / "junk" / "big.bin").write_text("x", encoding="utf-8")
    (root / "stale.pyc").write_text("x", encoding="utf-8")

    files = CodingService(root).tree()

    assert not any(f.startswith("junk/") for f in files)
    assert "stale.pyc" not in files
    assert ".gitignore" in files, "the ignore file itself is a real file"


def test_status_survives_a_repository_with_no_commits(tmp_path) -> None:
    """`rev-parse HEAD` fails with 128 when there is no HEAD, and status used
    to raise - leaving the header blank on exactly the repository that most
    needs an explanation."""
    import subprocess

    root = tmp_path / "unborn"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t.t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    status = CodingService(root).status()

    assert status["has_commits"] is False
    assert status["branch"], "a branch name, even an unborn one"
    assert status["name"] == "unborn"


def test_status_reports_commits_where_there_are_some(tmp_path) -> None:
    assert CodingService(_repo(tmp_path)).status()["has_commits"] is True


# -- the conversation ------------------------------------------------------


def test_a_task_carries_the_agent_answer(tmp_path) -> None:
    """The answer is the point of the exchange, and it was being computed and
    discarded: the panel showed a status, a commit count and a file list, and
    nothing the agent actually said."""
    service = CodingService(_repo(tmp_path))
    task = _branch_with_change(service, "forge/code_ans")
    task.answer = "Added a guard for division by zero."

    assert "answer" in task.to_dict()
    assert task.to_dict()["answer"] == "Added a guard for division by zero."


def test_the_rationale_reaches_the_transcript(tmp_path) -> None:
    """A tool name alone is a progress bar. The rationale is the model
    explaining itself, which is most of what makes an agent readable."""
    from forge.core.enums import EventType

    service = CodingService(_repo(tmp_path))
    task = Task(id="t", goal="g")
    hook = service._on_step(task)

    hook(EventType.PROPOSAL_RECEIVED,
         {"tool": "read_file", "rationale_summary": "Find the divide function."})
    hook(EventType.PROPOSAL_RECEIVED,
         {"tool": None, "rationale_summary": "The guard is in place."})

    kinds = [p["kind"] for p in task.progress]
    texts = [p["text"] for p in task.progress]

    assert kinds == ["plan", "think"]
    assert "read_file - Find the divide function." in texts[0]
    assert texts[1] == "The guard is in place."


def test_a_proposal_without_a_rationale_still_reports_the_tool(tmp_path) -> None:
    from forge.core.enums import EventType

    service = CodingService(_repo(tmp_path))
    task = Task(id="t", goal="g")
    service._on_step(task)(EventType.PROPOSAL_RECEIVED, {"tool": "edit_file"})

    assert task.progress[0]["text"] == "edit_file"


def test_a_new_chat_clears_the_transcript_and_the_memory(tmp_path) -> None:
    from forge.coding.memory import Turn

    service = CodingService(_repo(tmp_path))
    _branch_with_change(service, "forge/code_n1")
    service.conversation.record(Turn(goal="earlier", status="completed"))

    result = service.reset()

    assert result["cleared"] is True
    assert service.tasks == {} and service.order == []
    assert len(service.conversation) == 0


def test_a_new_chat_does_not_delete_anyone_s_branches(tmp_path) -> None:
    """Branches hold real commits. Silently deleting somebody's work because
    they wanted a clean chat would be the worst kind of surprise."""
    service = CodingService(_repo(tmp_path))
    _branch_with_change(service, "forge/code_keep")

    result = service.reset()

    assert result["branches_left_alone"] == 1
    assert "forge/code_keep" in service.agent.repo.run("branch")


def test_a_new_chat_is_refused_while_a_task_runs(tmp_path) -> None:
    service = CodingService(_repo(tmp_path))
    _start(service, "something")
    service.tasks[service.order[0]].status = "running"

    class _Busy:
        def done(self) -> bool:
            return False

    service._running = _Busy()  # type: ignore[assignment]
    with pytest.raises(HTTPException) as caught:
        service.reset()
    assert caught.value.status_code == 409


def test_status_reports_how_much_is_remembered(tmp_path) -> None:
    """A user who cannot see that context is carried has no way to know why a
    follow-up like "now do the same for modulo" worked."""
    from forge.coding.memory import Turn

    service = CodingService(_repo(tmp_path))
    assert service.status()["remembered"] == 0

    service.conversation.record(Turn(goal="first", status="completed"))
    assert service.status()["remembered"] == 1
