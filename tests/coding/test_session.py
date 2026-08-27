"""The interactive session.

Driven by feeding stdin, which is how a person drives it, so the tests
exercise the real loop rather than calling methods directly.

The properties that matter here are about *consent*: nothing reaches the
user's branch unless they said so, undo actually undoes, and the approval gate
defaults to no.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from forge.coding.session import Session
from forge.config import BudgetConfig, ForgeConfig, ProviderConfig
from tests.conftest import run

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(path, *args):
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
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


def _session(repo_path, script, inputs, monkeypatch):
    """A session whose model is scripted and whose stdin is a list of lines."""
    from forge.llm.mock import MockProvider

    queued = list(inputs)

    def fake_input(prompt: str = "") -> str:
        if not queued:
            raise EOFError
        return queued.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)

    config = ForgeConfig(
        database_url=f"sqlite:///{repo_path / '.forge' / 's.db'}",
        providers=(ProviderConfig(kind="mock"),),
        budget=BudgetConfig(max_steps=10),
    )

    import forge.coding.agent as agent_module

    real_forge = agent_module.Forge

    def patched(*a, **kw):
        kw["providers"] = [MockProvider(script)]
        return real_forge(*a, **kw)

    monkeypatch.setattr(agent_module, "Forge", patched)

    session = Session(repo=repo_path)
    original_start = session.start

    async def start_with_config() -> bool:
        result = await original_start()
        if session.agent is not None:
            session.agent.config = config
        return result

    session.start = start_with_config  # type: ignore[method-assign]
    return session


def tool(name, **args):
    return {"proposal": {"kind": "TOOL_CALL", "tool": name, "arguments": args,
                         "rationale_summary": "step"}}


def answer(text):
    return {"proposal": {"kind": "ANSWER", "answer": text, "rationale_summary": "done"}}


EDIT_SCRIPT = [
    tool("write_file", path="src/new.py", content="VALUE = 1\n"),
    answer("Added src/new.py."),
]


# ── the loop ────────────────────────────────────────────────────────────


def test_a_session_starts_and_exits_cleanly(repo, monkeypatch, capsys) -> None:
    session = _session(repo, EDIT_SCRIPT, ["/quit"], monkeypatch)
    assert run(session.loop()) == 0

    out = capsys.readouterr().out
    assert "interactive coding session" in out
    assert str(repo) in out


def test_an_unknown_command_does_not_end_the_session(repo, monkeypatch, capsys) -> None:
    session = _session(repo, EDIT_SCRIPT, ["/nonsense", "/quit"], monkeypatch)
    run(session.loop())
    assert "unknown command" in capsys.readouterr().out


def test_a_task_runs_and_reports(repo, monkeypatch, capsys) -> None:
    session = _session(repo, EDIT_SCRIPT, ["add a module", "/quit"], monkeypatch)
    run(session.loop())

    out = capsys.readouterr().out
    assert "write_file" in out, "progress must be narrated as it happens"
    assert "completed" in out
    assert len(session.turns) == 1
    assert session.turns[0].result is not None


# ── consent ─────────────────────────────────────────────────────────────


def test_nothing_reaches_your_branch_without_accept(repo, monkeypatch) -> None:
    """The core promise of the session."""
    session = _session(repo, EDIT_SCRIPT, ["add a module", "/quit"], monkeypatch)
    run(session.loop())

    assert session.agent is not None
    on_master = session.agent.repo.run("show", "master:src/new.py", check=False)
    assert "VALUE" not in on_master, "the edit must not be on the user's branch"
    assert not session.turns[0].accepted


def test_accept_merges_into_your_branch(repo, monkeypatch) -> None:
    session = _session(repo, EDIT_SCRIPT, ["add a module", "/accept", "/quit"], monkeypatch)
    run(session.loop())

    assert session.turns[0].accepted
    assert session.agent is not None
    assert "VALUE" in session.agent.repo.run("show", "master:src/new.py")


def test_undo_discards_the_branch_and_leaves_your_work(repo, monkeypatch) -> None:
    session = _session(repo, EDIT_SCRIPT, ["add a module", "/undo", "/quit"], monkeypatch)
    run(session.loop())

    assert session.turns[0].discarded
    assert session.agent is not None
    branches = session.agent.repo.run("branch", "--list", "forge/*")
    assert not branches.strip(), "the discarded branch must be gone"
    assert "VALUE" not in session.agent.repo.run("show", "master:src/new.py", check=False)
    assert (repo / "src" / "calc.py").exists(), "the user's files are untouched"


def test_accept_with_nothing_to_accept_is_harmless(repo, monkeypatch, capsys) -> None:
    session = _session(repo, EDIT_SCRIPT, ["/accept", "/quit"], monkeypatch)
    run(session.loop())
    assert "nothing to accept" in capsys.readouterr().out


def test_quitting_with_unmerged_work_says_where_it_is(repo, monkeypatch, capsys) -> None:
    """Leaving silently would strand a branch the user does not know about."""
    session = _session(repo, EDIT_SCRIPT, ["add a module", "/quit"], monkeypatch)
    run(session.loop())

    out = capsys.readouterr().out
    assert "left on a branch" in out
    assert "forge/" in out


# ── approval ────────────────────────────────────────────────────────────


def test_approval_defaults_to_refusal(repo, monkeypatch) -> None:
    """An operator who hits return without reading gets the safe outcome."""
    from forge.core.contracts import Action
    from forge.core.enums import SideEffect

    session = _session(repo, EDIT_SCRIPT, [], monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "")

    action = Action(
        run_id="r", step_id="s", tool="run_command", arguments={"command": "rm -rf /"},
        side_effect=SideEffect.IRREVERSIBLE_WRITE, idempotency_key="k", permit_id="p",
    )
    assert run(session._ask_approval(action)) is False


def test_approval_accepts_an_explicit_yes(repo, monkeypatch) -> None:
    from forge.core.contracts import Action
    from forge.core.enums import SideEffect

    session = _session(repo, EDIT_SCRIPT, [], monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    action = Action(
        run_id="r", step_id="s", tool="publish", arguments={},
        side_effect=SideEffect.IRREVERSIBLE_WRITE, idempotency_key="k", permit_id="p",
    )
    assert run(session._ask_approval(action)) is True


# ── display honesty ─────────────────────────────────────────────────────


def test_policy_shows_blocked_not_granted_when_isolation_is_short(repo, monkeypatch, capsys) -> None:
    """A capability granted in YAML but blocked by isolation must not read as granted.

    Showing `SHELL: granted` on a machine where it will actually be denied is
    the same class of confident-but-wrong display as a CLI that answers
    without a model.
    """
    session = _session(repo, EDIT_SCRIPT, ["/policy", "/quit"], monkeypatch)
    run(session.loop())

    out = capsys.readouterr().out
    assert "SHELL" in out
    if shutil.which("docker") is None:
        assert "BLOCKED" in out
        assert "needs container isolation" in out


def test_status_reports_the_sandbox_tier(repo, monkeypatch, capsys) -> None:
    session = _session(repo, EDIT_SCRIPT, ["/status", "/quit"], monkeypatch)
    run(session.loop())
    assert "confined" in capsys.readouterr().out
