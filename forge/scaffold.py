"""Project scaffolding for `forge init`.

A new user's first experience should be a working project, not a blank
directory and a README. This copies a starter config, a tools module, a policy
bundle and a case set into place, then tells them the next command to run.

Nothing here is clever on purpose: files are copied, never templated with
placeholder syntax the user then has to find and replace.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["TEMPLATES", "ScaffoldResult", "scaffold"]

TEMPLATES = Path(__file__).parent / "templates"


@dataclass
class ScaffoldResult:
    created: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    """Files that already existed. Never overwritten - `init` is safe to
    re-run in a project that has already been set up."""

    @property
    def anything_created(self) -> bool:
        return bool(self.created)


# (template relative path, destination relative path)
_LAYOUT: tuple[tuple[str, str], ...] = (
    ("forge.toml", "forge.toml"),
    ("tools.py", "tools.py"),
    ("policy.yaml", "policy.yaml"),
    ("cases/starter.yaml", "cases/starter.yaml"),
)

_GITIGNORE = """\
# FORGE runtime state - per-machine, never committed.
.forge/
*.db
*.db-wal
*.db-shm
reports/
"""


def scaffold(target: str | Path = ".", *, force: bool = False) -> ScaffoldResult:
    """Write starter files into `target`. Existing files are left alone."""
    root = Path(target).resolve()
    root.mkdir(parents=True, exist_ok=True)
    result = ScaffoldResult()

    for source_rel, dest_rel in _LAYOUT:
        source = TEMPLATES / source_rel
        dest = root / dest_rel
        if dest.exists() and not force:
            result.skipped.append(dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        result.created.append(dest)

    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE, encoding="utf-8")
        result.created.append(gitignore)
    elif ".forge/" not in gitignore.read_text(encoding="utf-8"):
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write("\n" + _GITIGNORE)
        result.created.append(gitignore)
    else:
        result.skipped.append(gitignore)

    return result
