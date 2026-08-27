"""The coding toolset.

Every tool is declared the way FORGE requires: typed arguments, a side-effect
class, a capability, and - for writes - a compensator. That declaration is
what makes a small model safe to point at a repository, because the runtime
enforces the boundaries the model cannot be trusted to respect.

Effect classes, and why each is what it is:

    READ                 list, read, search, git status/diff, run tests
    REVERSIBLE_WRITE     write_file, edit_file, delete_file - compensated by
                         a git restore, so a failed edit is undone by the
                         runtime rather than left for you to find
    IRREVERSIBLE_WRITE   run_command, git_push - ungranted by default and
                         requiring human approval when granted

`run_tests` is a READ even though it executes a process: it is the feedback
loop a coding agent lives on, and gating it behind approval would make the
agent useless. It runs a fixed command with no shell, which is the difference
between "runs your test suite" and "runs anything".
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from forge.coding.git import GitRepo
from forge.coding.workspace import Workspace
from forge.core.enums import RiskClass, SideEffect
from forge.errors import DeterministicError
from forge.tools.registry import ToolOutcome, ToolRegistry

__all__ = ["CodingContext", "build_coding_registry"]

MAX_OUTPUT_CHARS = 8_000


class CodingContext:
    """Shared state the tools close over: workspace, repo, and edit bookkeeping."""

    def __init__(
        self, workspace: Workspace, repo: GitRepo, sandbox: Any = None
    ) -> None:
        self.workspace = workspace
        self.repo = repo
        # Every command the agent runs goes through this. Defaults to the
        # local sandbox so a caller cannot accidentally construct a context
        # that executes commands with no confinement at all.
        from forge.sandbox import LocalSandbox

        self.sandbox = sandbox or LocalSandbox(workspace_root=workspace.root)
        self.files_touched: set[str] = set()
        self.tests_run = 0
        self.tests_passed: bool | None = None


# ── argument models ──────────────────────────────────────────────────────


class ListArgs(BaseModel):
    path: str = Field(default=".", description="Directory to list, relative to the repo.")


class ReadArgs(BaseModel):
    path: str = Field(description="File to read, relative to the repo root.")
    start_line: int = Field(default=1, ge=1, description="First line (1-indexed).")
    end_line: int = Field(default=0, ge=0, description="Last line; 0 means end of file.")


class SearchArgs(BaseModel):
    pattern: str = Field(description="Regex to search for.")
    glob: str = Field(default="", description="Optional path filter, e.g. '**/*.py'.")


class WriteArgs(BaseModel):
    path: str = Field(description="File to write, relative to the repo root.")
    content: str = Field(description="Full new contents of the file.")


class EditArgs(BaseModel):
    path: str = Field(description="File to edit.")
    old_text: str = Field(min_length=1, description="Exact text to replace. Must be unique.")
    new_text: str = Field(description="Replacement text.")


class DeleteArgs(BaseModel):
    path: str = Field(description="File to delete.")


class TestArgs(BaseModel):
    target: str = Field(default="", description="Optional path or test selector.")


class CommandArgs(BaseModel):
    command: str = Field(description="Shell-free command to run, e.g. 'npm run build'.")


def build_coding_registry(context: CodingContext) -> ToolRegistry:
    """Build a registry bound to one workspace and repository."""
    registry = ToolRegistry()
    ws = context.workspace
    repo = context.repo

    # ── reading ──────────────────────────────────────────────────────────

    @registry.tool(
        description="List files in a directory. Start here to orient yourself.",
        args=ListArgs,
        side_effect=SideEffect.READ,
        capability="CODE_READ",
    )
    async def list_files(path: str = ".") -> ToolOutcome:
        entries = ws.walk(path, limit=300)
        if not entries:
            return ToolOutcome(ok=True, output=f"{path} is empty or contains only ignored files")
        return ToolOutcome(ok=True, output="\n".join(entries[:300]))

    @registry.tool(
        description=(
            "Read a file. Prefer a line range for large files - you rarely need "
            "the whole thing."
        ),
        args=ReadArgs,
        side_effect=SideEffect.READ,
        capability="CODE_READ",
    )
    async def read_file(path: str, start_line: int = 1, end_line: int = 0) -> ToolOutcome:
        text = ws.read(path)
        lines = text.splitlines()
        last = end_line or len(lines)
        window = lines[start_line - 1 : last]
        numbered = "\n".join(
            f"{n:>5}| {line}" for n, line in enumerate(window, start=start_line)
        )
        return ToolOutcome(
            ok=True,
            output=_truncate(numbered),
            evidence={"path": path, "lines": len(lines), "shown": len(window)},
        )

    @registry.tool(
        description="Search the repository for a regex. Returns path:line matches.",
        args=SearchArgs,
        side_effect=SideEffect.READ,
        capability="CODE_READ",
    )
    async def search_code(pattern: str, glob: str = "") -> ToolOutcome:
        hits = ws.search(pattern, glob=glob)
        if not hits:
            return ToolOutcome(ok=True, output=f"no matches for {pattern!r}")
        rendered = "\n".join(f"{p}:{n}: {line}" for p, n, line in hits)
        return ToolOutcome(ok=True, output=_truncate(rendered), evidence={"hits": len(hits)})

    # ── writing ──────────────────────────────────────────────────────────
    #
    # Compensators restore the file from git. That is only sound because the
    # git session branches from a clean tree: the pre-edit content is always
    # recoverable from HEAD or from the step's own commit.

    async def _restore(path: str, **_: Any) -> None:
        repo.restore(path)

    @registry.tool(
        description=(
            "Replace an exact snippet in a file. Preferred over write_file: it "
            "fails loudly if the file is not what you assumed. old_text must be "
            "the raw source WITHOUT the '  12| ' line-number prefix that "
            "read_file displays."
        ),
        args=EditArgs,
        side_effect=SideEffect.REVERSIBLE_WRITE,
        capability="CODE_WRITE",
        risk=RiskClass.MEDIUM,
        compensate=_restore,
    )
    async def edit_file(path: str, old_text: str, new_text: str) -> ToolOutcome:
        original = ws.read(path)
        normalised = False

        occurrences = original.count(old_text)

        # `read_file` shows a line-number gutter, and models copy what they
        # were shown - gutter included. Instructing them not to does not work
        # reliably on small models, so the runtime absorbs the mistake instead
        # of failing an otherwise correct edit. The normalisation is recorded
        # in the evidence, so it is visible rather than silent.
        if occurrences == 0 and _has_gutter(old_text):
            stripped_old, stripped_new = _degutter(old_text), _degutter(new_text)
            if original.count(stripped_old) == 1:
                old_text, new_text = stripped_old, stripped_new
                occurrences, normalised = 1, True

        if occurrences == 0:
            # Deterministic: the same edit will fail the same way. The model
            # must read the file again rather than retry blindly.
            raise DeterministicError(
                f"old_text not found in {path}. Copy the exact source text - do "
                "not include the line-number prefix that read_file displays, and "
                "keep the original indentation.",
                path=path,
            )
        if occurrences > 1:
            raise DeterministicError(
                f"old_text appears {occurrences} times in {path}; it must be unique. "
                "Include surrounding lines to disambiguate.",
                path=path,
            )

        # Refuse an edit that has already been applied. A small model that
        # loses track of what it has done will re-insert the same block
        # repeatedly, and each insertion "succeeds" - producing a file with
        # three copies of the same function and a green test suite. Observed
        # live with qwen3:8b. Only multi-line blocks are checked, so adding a
        # second call to an existing function is unaffected.
        addition = new_text.replace(old_text, "", 1).strip()
        if "\n" in addition and len(addition) >= 20 and addition in original:
            raise DeterministicError(
                f"that change is already present in {path} - you have applied it "
                "before. Read the file to see its current state, then move on to "
                "the next part of the task.",
                path=path,
            )

        ws.write(path, original.replace(old_text, new_text, 1))
        context.files_touched.add(path)
        return ToolOutcome(
            ok=True,
            output=f"edited {path}" + (" (line numbers stripped)" if normalised else ""),
            evidence={"applied": True, "path": path, "normalised": normalised,
                      "delta_lines": new_text.count("\n") - old_text.count("\n")},
        )

    @registry.tool(
        description="Create a file, or replace one entirely. Use edit_file to change part of one.",
        args=WriteArgs,
        side_effect=SideEffect.REVERSIBLE_WRITE,
        capability="CODE_WRITE",
        risk=RiskClass.MEDIUM,
        compensate=_restore,
    )
    async def write_file(path: str, content: str) -> ToolOutcome:
        existed = ws.exists(path)
        ws.write(path, content)
        context.files_touched.add(path)
        return ToolOutcome(
            ok=True,
            output=f"{'rewrote' if existed else 'created'} {path} ({len(content)} bytes)",
            evidence={"applied": True, "path": path, "created": not existed},
        )

    @registry.tool(
        description="Delete a file.",
        args=DeleteArgs,
        side_effect=SideEffect.REVERSIBLE_WRITE,
        capability="CODE_WRITE",
        risk=RiskClass.HIGH,
        compensate=_restore,
    )
    async def delete_file(path: str) -> ToolOutcome:
        target = ws.resolve(path, must_exist=True)
        target.unlink()
        context.files_touched.add(path)
        return ToolOutcome(ok=True, output=f"deleted {path}",
                           evidence={"applied": True, "path": path})

    # ── feedback ─────────────────────────────────────────────────────────

    @registry.tool(
        description=(
            "Run the test suite and return the result. Use this to check your work "
            "before answering."
        ),
        args=TestArgs,
        side_effect=SideEffect.READ,
        capability="CODE_TEST",
        timeout_s=300.0,
    )
    async def run_tests(target: str = "") -> ToolOutcome:
        argv = _test_command(ws.root, target)
        if argv is None:
            return ToolOutcome(
                ok=False,
                error="no test runner detected (looked for pytest, package.json, cargo, go)",
            )
        code, output, result = await _exec(
            argv, cwd=ws.root, timeout_s=280, sandbox=context.sandbox
        )
        context.tests_run += 1
        context.tests_passed = code == 0
        note = f"\n[limits hit: {', '.join(result.limits_hit)}]" if result.limits_hit else ""
        return ToolOutcome(
            ok=True,  # the tool worked; whether tests passed is in the output
            output=_truncate(f"exit code {code}\n\n{_tail(output)}{note}"),
            evidence={
                "exit_code": code,
                "command": " ".join(argv),
                "isolation": result.isolation.label,
                "duration_ms": result.duration_ms,
                "limits_hit": result.limits_hit,
            },
        )

    @registry.tool(
        description="Show what has changed so far in this run.",
        args=ListArgs,
        side_effect=SideEffect.READ,
        capability="CODE_READ",
    )
    async def git_diff(path: str = ".") -> ToolOutcome:
        del path
        diff = repo.diff()
        return ToolOutcome(ok=True, output=_truncate(diff) or "no changes yet")

    # ── the dangerous one ────────────────────────────────────────────────

    @registry.tool(
        description=(
            "Run an arbitrary command. Irreversible - requires human approval, "
            "and is not granted by default."
        ),
        args=CommandArgs,
        side_effect=SideEffect.IRREVERSIBLE_WRITE,
        capability="SHELL",
        risk=RiskClass.HIGH,
        timeout_s=120.0,
        supports_dry_run=True,
    )
    async def run_command(command: str, _dry_run: bool = False) -> ToolOutcome:
        argv = shlex.split(command)
        if not argv:
            raise DeterministicError("empty command")
        if _dry_run:
            return ToolOutcome(ok=True, output=f"[dry-run] would run: {command}")
        code, output, result = await _exec(
            argv, cwd=ws.root, timeout_s=110, sandbox=context.sandbox
        )
        return ToolOutcome(
            ok=code == 0,
            output=_truncate(_tail(output)),
            error=None if code == 0 else f"exit code {code}",
            evidence={
                "applied": True, "command": command, "exit_code": code,
                "isolation": result.isolation.label, "limits_hit": result.limits_hit,
            },
        )

    return registry


# ── helpers ──────────────────────────────────────────────────────────────


# `[ \t]` rather than `\s`: `\s` also matches a newline, so on a gutter-only
# blank line (`    3|`) it would consume the line break and silently join the
# surrounding lines - producing a "stripped" text that no longer matches the
# file it came from.
_GUTTER = re.compile(r"^[ \t]*\d+\|[ \t]?", re.MULTILINE)


def _has_gutter(text: str) -> bool:
    """True when most non-empty lines carry a `NNN| ` prefix.

    Requiring a majority rather than any match matters: source that genuinely
    contains something like `x = 1| mask` must not be mangled.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    hits = sum(1 for line in lines if _GUTTER.match(line))
    return hits >= max(1, len(lines) * 2 // 3)


def _degutter(text: str) -> str:
    return _GUTTER.sub("", text)


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


def _tail(text: str, lines: int = 120) -> str:
    """Test output is most informative at the end - that is where failures are."""
    split = text.splitlines()
    if len(split) <= lines:
        return text
    return "... [earlier output omitted]\n" + "\n".join(split[-lines:])


def _test_command(root: Path, target: str) -> list[str] | None:
    """Detect the project's test runner. No shell, so nothing is interpolated."""
    import sys

    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() \
            or (root / "tests").is_dir():
        argv = [sys.executable, "-m", "pytest", "-q", "--no-header", "-x"]
        return [*argv, target] if target else argv
    if (root / "package.json").exists():
        return ["npm", "test", "--silent"]
    if (root / "Cargo.toml").exists():
        return ["cargo", "test", "--quiet"]
    if (root / "go.mod").exists():
        return ["go", "test", "./..."]
    return None


async def _exec(
    argv: list[str], *, cwd: Path, timeout_s: float, sandbox: Any,
    memory_mb: int = 2048,
) -> tuple[int, str, Any]:
    """Run a command through the sandbox. Never `subprocess` directly.

    Routing every execution through one place is what makes the isolation
    claim checkable: there is a single call site to audit, and a tool that
    wanted to bypass it would have to import subprocess itself - which the
    adversarial tests assert none of them do.
    """
    from forge.sandbox import SandboxLimits, SandboxSpec

    result = await sandbox.run(
        SandboxSpec(
            argv=tuple(argv),
            cwd=cwd,
            limits=SandboxLimits(
                wall_clock_s=timeout_s,
                memory_mb=memory_mb,
                max_output_bytes=MAX_OUTPUT_CHARS * 4,
                network=False,
            ),
        )
    )
    return result.exit_code, result.output, result
