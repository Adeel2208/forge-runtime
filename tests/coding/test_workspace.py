"""Workspace confinement.

This is the security boundary of the coding agent: an agent that can write
outside the repository, into `.git`, or read `.env` is not one you can point
at a real project. "The model probably won't" is not a boundary, so these
tests try to escape on purpose.
"""

from __future__ import annotations

import pytest

from forge.coding.workspace import Workspace, WorkspaceError
from forge.errors import PolicyDenied


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "import os\n\n\ndef main():\n    return 1\n\n\nclass Runner:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# project\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET_KEY=hunter2\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    return Workspace(tmp_path)


# ── escaping ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "attempt",
    [
        "../outside.txt",
        "../../etc/passwd",
        "src/../../escape.py",
        "src/../../../etc/shadow",
        "./../../nope",
    ],
)
def test_relative_escapes_are_refused(ws, attempt) -> None:
    """`..` must be resolved before the check, not after."""
    with pytest.raises(PolicyDenied, match="escapes the workspace"):
        ws.resolve(attempt)


def test_absolute_paths_outside_are_refused(ws, tmp_path) -> None:
    outside = tmp_path.parent / "elsewhere.txt"
    with pytest.raises(PolicyDenied, match="escapes the workspace"):
        ws.resolve(str(outside))


def test_absolute_path_inside_is_allowed(ws) -> None:
    """Confinement is about location, not about the shape of the string."""
    resolved = ws.resolve(str(ws.root / "README.md"), must_exist=True)
    assert resolved.name == "README.md"


def test_symlink_pointing_outside_is_refused(ws, tmp_path) -> None:
    """A symlink is the escape that a naive prefix check misses."""
    secret = tmp_path.parent / "outside_secret.txt"
    secret.write_text("classified", encoding="utf-8")
    link = ws.root / "innocent.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this platform")

    with pytest.raises(PolicyDenied, match="escapes the workspace"):
        ws.resolve("innocent.txt")


# ── protected paths ─────────────────────────────────────────────────────


@pytest.mark.parametrize("protected", [".git", ".git/config", ".env", "node_modules/junk.js"])
def test_protected_paths_are_refused(ws, protected) -> None:
    with pytest.raises(PolicyDenied, match="protected"):
        ws.resolve(protected)


def test_secrets_are_invisible_to_reads(ws) -> None:
    """A file the agent cannot read is a file it cannot leak into a prompt."""
    with pytest.raises(PolicyDenied):
        ws.read(".env")
    assert ".env" not in ws.walk()
    assert "SECRET_KEY" not in ws.repo_map()


def test_git_internals_are_not_listed(ws) -> None:
    """An agent that can rewrite .git can undo every git-based safety property."""
    listing = ws.walk()
    assert not any(f.startswith(".git") for f in listing)


def test_ignored_directories_are_not_walked(ws) -> None:
    assert not any("node_modules" in f for f in ws.walk())


# ── ordinary operation ──────────────────────────────────────────────────


def test_read_and_write_round_trip(ws) -> None:
    ws.write("src/new.py", "print('hi')\n")
    assert ws.read("src/new.py") == "print('hi')\n"
    assert "src/new.py" in ws.walk()


def test_write_creates_parent_directories(ws) -> None:
    ws.write("a/b/c/deep.py", "x = 1\n")
    assert ws.read("a/b/c/deep.py") == "x = 1\n"


def test_missing_file_is_a_deterministic_error(ws) -> None:
    """Retrying an unchanged read of a missing file will fail identically."""
    with pytest.raises(WorkspaceError, match="no such file"):
        ws.resolve("src/ghost.py", must_exist=True)


def test_oversized_file_is_refused_with_advice(ws) -> None:
    ws.write("big.py", "x\n" * 10_000)
    small = Workspace(ws.root, max_read_bytes=100)
    with pytest.raises(WorkspaceError, match="Read a range"):
        small.read("big.py")


def test_binary_file_is_refused(ws) -> None:
    (ws.root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    with pytest.raises(WorkspaceError, match="not UTF-8"):
        ws.read("image.png")


def test_empty_path_is_refused(ws) -> None:
    with pytest.raises(WorkspaceError, match="empty path"):
        ws.resolve("   ")


# ── the repo map ────────────────────────────────────────────────────────


def test_repo_map_lists_files_and_symbols(ws) -> None:
    """The map is what lets a small model choose a file instead of guessing."""
    rendered = ws.repo_map()
    assert "src/main.py" in rendered
    assert "main" in rendered
    assert "Runner" in rendered


def test_repo_map_survives_a_syntax_error(ws) -> None:
    """It must work mid-edit, which is exactly when the file is broken."""
    ws.write("src/broken.py", "def oops(:\n    this is not python\n")
    rendered = ws.repo_map()
    assert "src/broken.py" in rendered


def test_repo_map_is_bounded(ws) -> None:
    for i in range(60):
        ws.write(f"src/mod{i}.py", "def f():\n    pass\n")
    rendered = ws.repo_map(max_files=10)
    assert "more source files" in rendered


# ── search ──────────────────────────────────────────────────────────────


def test_search_finds_matches_with_locations(ws) -> None:
    hits = ws.search(r"def main")
    assert hits
    path, line, text = hits[0]
    assert path == "src/main.py"
    assert line == 4
    assert "def main" in text


def test_search_never_returns_ignored_files(ws) -> None:
    hits = ws.search("SECRET_KEY")
    assert hits == []


def test_search_rejects_a_bad_pattern(ws) -> None:
    with pytest.raises(WorkspaceError, match="invalid search pattern"):
        ws.search("(unclosed")
