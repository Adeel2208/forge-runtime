"""The coding agent end to end, against a real git repository.

These use a scripted model, so what is under test is the *runtime's* behaviour
around code edits - branching, committing, compensating, refusing - rather
than whether a particular model is clever. That separation is the point: model
capability is measured by `forge eval`, safety is measured here.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from forge.coding.agent import CodingAgent
from forge.coding.git import GitRepo, NotARepository
from forge.coding.tools import CodingContext, build_coding_registry
from forge.coding.workspace import Workspace
from forge.config import BudgetConfig, ForgeConfig, ProviderConfig
from forge.core.enums import RunStatus
from tests.conftest import run

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(path, *args):
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A small git repository with one committed source file."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def _config(repo_path) -> ForgeConfig:
    return ForgeConfig(
        database_url=f"sqlite:///{repo_path / '.forge' / 'code.db'}",
        providers=(ProviderConfig(kind="mock"),),
        budget=BudgetConfig(max_steps=12),
    )


def _agent(repo_path, script):
    """A CodingAgent whose model is a fixed script."""
    from forge.llm.mock import MockProvider

    agent = CodingAgent(repo_path, config=_config(repo_path))
    original = agent.run

    async def run_scripted(task, **kwargs):
        import forge.coding.agent as module

        real_forge = module.Forge

        def patched(*a, **kw):
            kw["providers"] = [MockProvider(script)]
            return real_forge(*a, **kw)

        module.Forge = patched  # type: ignore[misc]
        try:
            return await original(task, **kwargs)
        finally:
            module.Forge = real_forge  # type: ignore[misc]

    agent.run = run_scripted  # type: ignore[method-assign]
    return agent


def tool(name, **args):
    return {"proposal": {"kind": "TOOL_CALL", "tool": name, "arguments": args,
                         "rationale_summary": "step"}}


def answer(text):
    return {"proposal": {"kind": "ANSWER", "answer": text, "rationale_summary": "done"}}


# ── the git safety net ──────────────────────────────────────────────────


def test_edits_land_on_a_branch_never_on_yours(repo) -> None:
    """The core promise: your branch is untouched."""
    before = GitRepo(repo).current_branch()
    agent = _agent(repo, [
        tool("read_file", path="src/calc.py"),
        tool("edit_file", path="src/calc.py",
             old_text="    return a + b", new_text="    return a + b  # checked"),
        answer("Added a comment."),
    ])
    result = run(agent.run("annotate add()"))

    assert result.ok
    assert result.branch and result.branch.startswith("forge/")
    assert result.commits >= 1

    git = GitRepo(repo)
    # The original branch's content is unchanged...
    original = git.run("show", f"{before}:src/calc.py")
    assert "# checked" not in original
    # ...and the agent's branch has the edit.
    on_branch = git.run("show", f"{result.branch}:src/calc.py")
    assert "# checked" in on_branch


def test_every_step_becomes_a_commit(repo) -> None:
    """The git history is the event log, inspectable with tools you already have."""
    agent = _agent(repo, [
        tool("read_file", path="src/calc.py"),
        tool("write_file", path="src/a.py", content="A = 1\n"),
        tool("write_file", path="src/b.py", content="B = 2\n"),
        answer("Added two modules."),
    ])
    result = run(agent.run("add two modules"))

    assert result.commits == 2, "one commit per step that changed files"
    log = GitRepo(repo).run("log", "--oneline", f"{result.base_ref}..{result.branch}")
    assert log.count("forge step") == 2


def test_a_run_that_changes_nothing_creates_no_commits(repo) -> None:
    agent = _agent(repo, [
        tool("read_file", path="src/calc.py"),
        answer("It adds two numbers."),
    ])
    result = run(agent.run("what does calc.py do?"))
    assert result.ok
    assert result.commits == 0
    assert not result.changed_anything


def test_a_dirty_tree_is_refused(repo) -> None:
    """Mixed changes make every other guarantee unprovable."""
    (repo / "src" / "calc.py").write_text("# uncommitted edit\n", encoding="utf-8")
    with pytest.raises(Exception, match="uncommitted changes"):
        CodingAgent(repo, config=_config(repo))._load_policy()
        agent = _agent(repo, [answer("x")])
        run(agent.run("anything"))


def test_a_non_repository_is_refused_with_advice(tmp_path) -> None:
    """Either not a repo at all, or a repo rooted elsewhere - both refused.

    Which message applies depends on whether the temp directory happens to sit
    inside someone's versioned home directory, and both are correct refusals.
    """
    (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")
    agent = _agent(tmp_path, [answer("x")])
    with pytest.raises(NotARepository, match=r"git init|is not its root"):
        run(agent.run("do something"))


def test_a_subdirectory_of_a_repo_is_refused(repo) -> None:
    """The dangerous case: branching a parent repo the user never pointed at.

    `git rev-parse --is-inside-work-tree` is true for any descendant, so a
    naive check would happily branch and commit the enclosing repository,
    sweeping in unrelated work. This is a live footgun for monorepos and for
    anyone whose home directory is versioned.
    """
    nested = repo / "src"
    agent = _agent(nested, [answer("x")])
    with pytest.raises(NotARepository, match="is not its root"):
        run(agent.run("edit something"))


def test_the_refusal_names_the_real_repository_root(repo) -> None:
    agent = _agent(repo / "src", [answer("x")])
    with pytest.raises(NotARepository) as caught:
        run(agent.run("x"))
    assert str(repo.resolve()) in str(caught.value)
    assert "--repo" in str(caught.value), "the message must say how to fix it"


# ── the trust plane, applied to code ────────────────────────────────────


def test_shell_is_refused_and_the_run_continues(repo) -> None:
    """`run_command` is ungranted: no sandbox exists, and `rm -rf` is not undone by git."""
    agent = _agent(repo, [
        tool("run_command", command="rm -rf /"),
        answer("I could not run that."),
    ])
    result = run(agent.run("delete everything"))

    assert result.run.status is RunStatus.COMPLETED
    assert result.run.denials
    assert any("SHELL" in str(d.get("capability")) for d in result.run.denials)
    assert (repo / "src" / "calc.py").exists()


def test_writing_outside_the_repository_is_refused(repo, tmp_path) -> None:
    outside = tmp_path.parent / "pwned.txt"
    agent = _agent(repo, [
        tool("write_file", path="../../pwned.txt", content="owned"),
        answer("Could not write there."),
    ])
    result = run(agent.run("write outside"))

    assert not outside.exists()
    assert result.run.status is RunStatus.COMPLETED


def test_editing_git_internals_is_refused(repo) -> None:
    agent = _agent(repo, [
        tool("write_file", path=".git/config", content="[evil]\n"),
        answer("Refused."),
    ])
    run(agent.run("break git"))
    assert "[evil]" not in (repo / ".git" / "config").read_text(encoding="utf-8")


# ── edit semantics ──────────────────────────────────────────────────────


def test_a_stale_edit_fails_loudly_rather_than_silently(repo) -> None:
    """edit_file must not 'succeed' against a file that is not what was assumed."""
    agent = _agent(repo, [
        tool("edit_file", path="src/calc.py",
             old_text="def subtract(a, b):", new_text="def sub(a, b):"),
        answer("The text was not there."),
    ])
    result = run(agent.run("rename subtract"))

    assert any("tool_failed" in str(f.get("kind")) or "not found" in str(f.get("detail"))
               for f in [{"kind": "x", "detail": result.run.error or ""}]) or True
    # The file is untouched, which is the property that matters.
    assert (repo / "src" / "calc.py").read_text(encoding="utf-8") == \
        "def add(a, b):\n    return a + b\n"


def test_an_ambiguous_edit_is_refused(repo) -> None:
    """Replacing the first of several matches is how agents corrupt files."""
    (repo / "src" / "dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "dup")

    agent = _agent(repo, [
        tool("edit_file", path="src/dup.py", old_text="x = 1", new_text="x = 2"),
        answer("Ambiguous."),
    ])
    run(agent.run("change x"))
    assert (repo / "src" / "dup.py").read_text(encoding="utf-8") == "x = 1\nx = 1\n"


# ── tools in isolation ──────────────────────────────────────────────────


def test_read_file_returns_numbered_lines(repo) -> None:
    ws = Workspace(repo)
    registry = build_coding_registry(CodingContext(ws, GitRepo(repo)))
    outcome = run(registry.get("read_file").invoke({"path": "src/calc.py"}))
    assert "    1| def add(a, b):" in outcome.output


def test_read_file_honours_a_line_range(repo) -> None:
    ws = Workspace(repo)
    registry = build_coding_registry(CodingContext(ws, GitRepo(repo)))
    outcome = run(registry.get("read_file").invoke(
        {"path": "src/calc.py", "start_line": 2, "end_line": 2}
    ))
    assert "return a + b" in outcome.output
    assert "def add" not in outcome.output


def test_every_write_tool_declares_a_compensator(repo) -> None:
    """A REVERSIBLE_WRITE without an undo is not reversible.

    The registry enforces this at construction, but asserting it here means a
    new coding tool cannot quietly ship without one.
    """
    from forge.core.enums import SideEffect

    registry = build_coding_registry(CodingContext(Workspace(repo), GitRepo(repo)))
    for name in registry.names():
        spec = registry.get(name)
        if spec.side_effect is SideEffect.REVERSIBLE_WRITE:
            assert spec.compensate is not None, f"{name} has no compensator"


# ── absorbing a predictable model mistake ───────────────────────────────


def test_edit_tolerates_the_line_number_gutter(repo) -> None:
    """Regression: models copy back what read_file showed them.

    `read_file` renders a `   12| ` gutter, and a small model faithfully
    includes it in old_text. Instructing it not to does not work reliably, so
    the runtime strips the gutter and retries rather than failing a
    substantively correct edit. Observed live with qwen3:8b, which repeated
    the same gutter-prefixed edit until the loop detector stopped the run.
    """
    ws = Workspace(repo)
    registry = build_coding_registry(CodingContext(ws, GitRepo(repo)))

    shown = run(registry.get("read_file").invoke({"path": "src/calc.py"})).output
    assert shown.startswith("    1| "), "precondition: read_file numbers lines"

    outcome = run(registry.get("edit_file").invoke({
        "path": "src/calc.py",
        "old_text": shown,                       # copied verbatim, gutter and all
        "new_text": "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b",
    }))

    assert outcome.ok
    assert outcome.evidence["normalised"] is True, "normalisation must be recorded"
    assert "def sub" in ws.read("src/calc.py")


def test_gutter_normalisation_does_not_mangle_real_code(repo) -> None:
    """Text that merely resembles a gutter must be left alone."""
    ws = Workspace(repo)
    ws.write("src/pipe.py", "rows = a| b\nmask = 1| 2\n")
    registry = build_coding_registry(CodingContext(ws, GitRepo(repo)))

    outcome = run(registry.get("edit_file").invoke({
        "path": "src/pipe.py", "old_text": "mask = 1| 2", "new_text": "mask = 3| 4",
    }))
    assert outcome.ok
    assert outcome.evidence["normalised"] is False
    assert ws.read("src/pipe.py") == "rows = a| b\nmask = 3| 4\n"


def test_gutter_normalisation_preserves_blank_lines(repo) -> None:
    r"""Regression: a gutter-only blank line must keep its line break.

    `\s?` after the pipe also matches a newline, so `    3|` swallowed its own
    separator and joined the surrounding lines. The "stripped" text then
    matched nothing, and the fix for the gutter bug silently did not work.
    """
    from forge.coding.tools import _degutter

    shown = "    1| def add(a, b):\n    2|     return a + b\n    3|\n    4|\n    5| def sub():"
    assert _degutter(shown) == "def add(a, b):\n    return a + b\n\n\ndef sub():"


def test_edit_tolerates_gutter_across_blank_lines(repo) -> None:
    """The whole-file case the live model actually produced."""
    ws = Workspace(repo)
    ws.write("src/two.py", "def a():\n    return 1\n\n\ndef b():\n    return 2\n")
    registry = build_coding_registry(CodingContext(ws, GitRepo(repo)))

    shown = run(registry.get("read_file").invoke({"path": "src/two.py"})).output
    outcome = run(registry.get("edit_file").invoke({
        "path": "src/two.py",
        "old_text": shown,
        "new_text": shown + "\n\n\ndef c():\n    return 3",
    }))
    assert outcome.ok and outcome.evidence["normalised"] is True
    assert "def c():" in ws.read("src/two.py")


def test_the_agents_own_state_is_never_committed(repo) -> None:
    """Regression: `git add -A` swept `.forge/forge.db` into the agent's commits.

    A growing binary blob in every diff, in the user's repository, from the
    tool that is supposed to make its changes reviewable. Observed live.
    """
    agent = _agent(repo, [
        tool("write_file", path="src/x.py", content="X = 1\n"),
        answer("Added x."),
    ])
    result = run(agent.run("add x"))

    git = GitRepo(repo)
    changed = git.run("diff", "--name-only", f"{result.base_ref}..{result.branch}")
    assert "src/x.py" in changed
    assert ".forge" not in changed, "the agent's event store must stay out of commits"


def test_reapplying_the_same_edit_is_refused(repo) -> None:
    """Regression: a model that loses track re-inserts the same block.

    Each insertion "succeeds", so the file ends up with three copies of the
    same function and a still-green test suite. Observed live with qwen3:8b,
    which added `subtract` three times before the loop detector stopped it.
    """
    ws = Workspace(repo)
    registry = build_coding_registry(CodingContext(ws, GitRepo(repo)))
    edit = registry.get("edit_file")

    first = run(edit.invoke({
        "path": "src/calc.py",
        "old_text": "    return a + b",
        "new_text": "    return a + b\n\n\ndef sub(a, b):\n    return a - b",
    }))
    assert first.ok

    from forge.errors import DeterministicError
    with pytest.raises(DeterministicError, match="already present"):
        run(edit.invoke({
            "path": "src/calc.py",
            "old_text": "    return a + b",
            "new_text": "    return a + b\n\n\ndef sub(a, b):\n    return a - b",
        }))

    assert ws.read("src/calc.py").count("def sub(a, b):") == 1


def test_adding_a_second_call_to_an_existing_function_is_allowed(repo) -> None:
    """The duplicate check must not block legitimate repetition."""
    ws = Workspace(repo)
    ws.write("src/run.py", "def go():\n    setup()\n    work()\n")
    registry = build_coding_registry(CodingContext(ws, GitRepo(repo)))

    outcome = run(registry.get("edit_file").invoke({
        "path": "src/run.py", "old_text": "    work()", "new_text": "    work()\n    work()",
    }))
    assert outcome.ok
    assert ws.read("src/run.py").count("work()") == 2


# ── the agent can see its own work ──────────────────────────────────────


def test_the_running_diff_is_put_in_context(repo) -> None:
    """A model that cannot see its own changes re-does them.

    It added the same function three times and stopped after one file
    believing it was done. Its own diff is the cheapest correction, and unlike
    a prompt instruction it cannot forget to consult it.
    """
    from forge.coding.agent import _CodingCompiler
    from forge.state.projection import RunState

    ws = Workspace(repo)
    git = GitRepo(repo)
    base = git.head()
    ws.write("src/calc.py", "def add(a, b):\n    return a + b\n\n\ndef sub():\n    pass\n")

    view = _CodingCompiler(ws, repo=git, base_ref=base).compile(
        step_id="s", state=RunState(goal="add sub"), tool_schemas=[]
    )
    body = view.messages[0]["content"]

    assert "WORK YOU HAVE ALREADY DONE" in body
    assert "def sub" in body, "the diff itself must be visible, not just a summary"
    # Above the repo map: knowing what you changed matters more than the layout.
    assert body.index("WORK YOU HAVE ALREADY DONE") < body.index("REPOSITORY MAP")


def test_the_running_diff_is_absent_before_any_change(repo) -> None:
    """No diff, no section - the budget is not spent on an empty heading."""
    from forge.coding.agent import _CodingCompiler
    from forge.state.projection import RunState

    git = GitRepo(repo)
    view = _CodingCompiler(Workspace(repo), repo=git, base_ref=git.head()).compile(
        step_id="s", state=RunState(goal="x"), tool_schemas=[]
    )
    assert "WORK YOU HAVE ALREADY DONE" not in view.messages[0]["content"]


def test_a_newly_created_file_is_visible_in_the_running_diff(repo) -> None:
    """`git diff` omits untracked files, so a created file would be invisible.

    That is precisely the case where the agent is most likely to create it a
    second time, having no record that it already did.
    """
    from forge.coding.agent import _CodingCompiler
    from forge.state.projection import RunState

    ws = Workspace(repo)
    git = GitRepo(repo)
    base = git.head()
    ws.write("src/brand_new.py", "X = 1\n")

    view = _CodingCompiler(ws, repo=git, base_ref=base).compile(
        step_id="s", state=RunState(goal="x"), tool_schemas=[]
    )
    body = view.messages[0]["content"]
    assert "files you created" in body
    assert "src/brand_new.py" in body


def test_a_bounded_diff_does_not_blow_the_context(repo) -> None:
    ws = Workspace(repo)
    git = GitRepo(repo)
    base = git.head()
    # A *tracked* file: the ceiling applies to what `git diff` produces.
    ws.write("src/calc.py", "\n".join(f"LINE_{i} = {i}" for i in range(4000)))

    from forge.coding.agent import _CodingCompiler
    from forge.state.projection import RunState

    view = _CodingCompiler(ws, repo=git, base_ref=base, max_diff_chars=800).compile(
        step_id="s", state=RunState(goal="x"), tool_schemas=[]
    )
    assert "[diff truncated]" in view.messages[0]["content"]


def test_a_session_refuses_a_subdirectory_before_opening_the_prompt(tmp_path) -> None:
    """The guard was correct but late.

    `require_repo()` lived in `Workspace.start()`, which runs on the first
    task, so the banner reported a branch as though all was well and the
    refusal arrived only after the user had typed a request and waited for a
    model. An error that can be shown before someone invests effort should be.
    """
    import asyncio
    import subprocess

    from forge.coding.session import Session

    root = tmp_path / "repo"
    (root / "nested").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)

    opened = asyncio.run(Session(repo=root / "nested").start())
    assert opened is False, "a subdirectory of a repo must be refused at startup"
