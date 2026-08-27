"""The workspace: a confined view of one repository.

Every path an agent touches passes through here first. That is the whole
point - a coding agent is one bad path away from writing outside the project,
and "the model probably won't" is not a security boundary.

Confinement rules, in order:

* the path is resolved (symlinks included) before anything else;
* the resolved path must be inside the workspace root;
* `.git/` is off limits - an agent that can rewrite git internals can undo
  every safety property the git session provides;
* ignored paths (`.env`, `node_modules`, build output) are invisible to reads
  and refused for writes.

The last one is not only tidiness. Secrets live in `.env`, and a model that
cannot read a file cannot leak it into a prompt, a log, or a commit message.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

from forge.errors import DeterministicError, PolicyDenied

__all__ = ["DEFAULT_IGNORES", "Workspace", "WorkspaceError"]


class WorkspaceError(DeterministicError):
    """A path that cannot be used. Deterministic: retrying will not help."""


# Never read, never write, never list. Ordered roughly by how bad it would be.
DEFAULT_IGNORES: tuple[str, ...] = (
    ".git", ".git/**",
    ".env", ".env.*", "*.pem", "*.key", "id_rsa*", ".npmrc", ".pypirc",
    "**/__pycache__/**", "*.pyc",
    "node_modules/**", ".venv/**", "venv/**", ".tox/**",
    "dist/**", "build/**", "*.egg-info/**",
    ".mypy_cache/**", ".pytest_cache/**", ".ruff_cache/**", ".forge/**",
    "*.db", "*.db-wal", "*.db-shm",
)

# Source files worth showing in a repo map. Anything else is listed but not read.
_CODE_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
    ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".cs", ".swift", ".scala",
    ".sh", ".sql", ".toml", ".yaml", ".yml", ".json", ".md",
})

MAX_READ_BYTES = 400_000


@dataclass
class Workspace:
    """A confined handle on a project directory."""

    root: Path
    ignores: tuple[str, ...] = DEFAULT_IGNORES
    max_read_bytes: int = MAX_READ_BYTES
    _resolved_root: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve()
        if not root.is_dir():
            raise WorkspaceError(f"workspace root is not a directory: {root}")
        self.root = root
        self._resolved_root = root

    # -- confinement -------------------------------------------------------

    def resolve(self, relative: str, *, must_exist: bool = False) -> Path:
        """Resolve a path and prove it is inside the workspace.

        Raises `PolicyDenied` for an escape attempt rather than
        `WorkspaceError`: leaving the workspace is a boundary violation, and
        it should read as one in the event log.
        """
        if not relative or not relative.strip():
            raise WorkspaceError("empty path")

        candidate = Path(relative.strip())
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self._resolved_root / candidate).resolve()

        # `resolve()` first, compare second. Comparing the *unresolved* path
        # would miss `a/../../etc/passwd` and every symlink pointing outward.
        if not self._within(resolved):
            raise PolicyDenied(
                f"path escapes the workspace: {relative!r}",
                reason="workspace confinement",
                resolved=str(resolved),
                root=str(self._resolved_root),
            )

        rel = resolved.relative_to(self._resolved_root).as_posix()
        if self.is_ignored(rel):
            raise PolicyDenied(
                f"path is protected and cannot be accessed: {rel}",
                reason="protected path",
            )

        if must_exist and not resolved.exists():
            raise WorkspaceError(f"no such file: {rel}")
        return resolved

    def _within(self, resolved: Path) -> bool:
        try:
            resolved.relative_to(self._resolved_root)
        except ValueError:
            return False
        return True

    def is_ignored(self, rel_posix: str) -> bool:
        if not rel_posix or rel_posix == ".":
            return False
        for pattern in self.ignores:
            if fnmatch.fnmatch(rel_posix, pattern):
                return True
            # Match a directory prefix too: `.git` should hide `.git/config`
            # without every caller having to spell out `**`.
            head = pattern.rstrip("/*")
            if head and (rel_posix == head or rel_posix.startswith(head + "/")):
                return True
        return False

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self._resolved_root).as_posix()

    # -- reading -----------------------------------------------------------

    def read(self, relative: str) -> str:
        path = self.resolve(relative, must_exist=True)
        if path.is_dir():
            raise WorkspaceError(f"{relative} is a directory")
        size = path.stat().st_size
        if size > self.max_read_bytes:
            raise WorkspaceError(
                f"{relative} is {size} bytes, over the {self.max_read_bytes} limit. "
                "Read a range instead."
            )
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(f"{relative} is not UTF-8 text") from exc

    def write(self, relative: str, content: str) -> Path:
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def exists(self, relative: str) -> bool:
        try:
            return self.resolve(relative).exists()
        except (WorkspaceError, PolicyDenied):
            return False

    # -- listing -----------------------------------------------------------

    def walk(self, subdir: str = ".", *, limit: int = 2000) -> list[str]:
        """Every non-ignored file under `subdir`, workspace-relative."""
        start = self.resolve(subdir) if subdir not in (".", "") else self._resolved_root
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(start):
            here = Path(dirpath)
            # Prune ignored directories in place so os.walk does not descend.
            dirnames[:] = [
                d for d in sorted(dirnames)
                if not self.is_ignored((here / d).resolve()
                                       .relative_to(self._resolved_root).as_posix())
            ]
            for name in sorted(filenames):
                rel = (here / name).resolve().relative_to(self._resolved_root).as_posix()
                if self.is_ignored(rel):
                    continue
                found.append(rel)
                if len(found) >= limit:
                    return found
        return found

    # -- repo map ----------------------------------------------------------

    def repo_map(self, *, max_files: int = 120, max_symbols_per_file: int = 12) -> str:
        """A compact structural summary of the project.

        Small models cannot hold a repository in context, and dumping files
        wastes the budget on syntax they do not need. A map of paths plus
        top-level symbols lets the model choose what to open, which is the
        decision it is actually good at.
        """
        files = self.walk(limit=max_files * 4)
        code = [f for f in files if Path(f).suffix in _CODE_SUFFIXES]
        other = [f for f in files if f not in set(code)]

        lines: list[str] = [f"# REPOSITORY MAP  ({len(files)} files)"]
        for rel in code[:max_files]:
            symbols = self._symbols(rel, limit=max_symbols_per_file)
            if symbols:
                lines.append(f"{rel}: {', '.join(symbols)}")
            else:
                lines.append(rel)

        if len(code) > max_files:
            lines.append(f"... and {len(code) - max_files} more source files")
        if other:
            shown = ", ".join(other[:20])
            lines.append(f"\nother files: {shown}"
                         + (f" ... (+{len(other) - 20})" if len(other) > 20 else ""))
        return "\n".join(lines)

    def _symbols(self, rel: str, *, limit: int) -> list[str]:
        """Top-level definitions, by cheap textual scan.

        Deliberately not a parser: a repo map that fails on a syntax error is
        useless exactly when the agent needs it most - mid-edit.
        """
        try:
            text = self.read(rel)
        except (WorkspaceError, PolicyDenied, OSError):
            return []

        suffix = Path(rel).suffix
        prefixes: tuple[str, ...]
        if suffix in (".py", ".pyi"):
            prefixes = ("def ", "class ", "async def ")
        elif suffix in (".js", ".jsx", ".ts", ".tsx"):
            prefixes = ("function ", "class ", "export function ", "export class ",
                        "export const ", "const ")
        elif suffix in (".go",):
            prefixes = ("func ", "type ")
        elif suffix in (".rs",):
            prefixes = ("fn ", "pub fn ", "struct ", "pub struct ", "impl ")
        else:
            return []

        out: list[str] = []
        for line in text.splitlines():
            if line[:1].isspace():
                continue  # top level only
            for prefix in prefixes:
                if line.startswith(prefix):
                    name = line[len(prefix):].split("(")[0].split(":")[0]
                    name = name.split("=")[0].split("{")[0].strip()
                    if name:
                        out.append(name)
                    break
            if len(out) >= limit:
                out.append("...")
                break
        return out

    # -- search ------------------------------------------------------------

    def search(
        self, pattern: str, *, glob: str = "", limit: int = 60, ignore_case: bool = True
    ) -> list[tuple[str, int, str]]:
        """Literal-or-regex search. Returns (path, line number, line)."""
        import re

        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise WorkspaceError(f"invalid search pattern: {exc}") from exc

        hits: list[tuple[str, int, str]] = []
        for rel in self.walk():
            if glob and not fnmatch.fnmatch(rel, glob):
                continue
            if Path(rel).suffix not in _CODE_SUFFIXES:
                continue
            try:
                text = self.read(rel)
            except (WorkspaceError, PolicyDenied, OSError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append((rel, number, line.strip()[:200]))
                    if len(hits) >= limit:
                        return hits
        return hits
