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
