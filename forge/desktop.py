"""Desktop integration: a shortcut that launches Studio on a repository.

The browser's own "Install" turns Studio into a windowed app with an icon and a
Start Menu entry. This is the other half - a launcher that starts the server
first, so the icon works from a cold machine rather than only while something
is already running.

Nothing here is required. It is written per-platform because there is no
portable way to make an operating system offer to launch a program, and every
path is best-effort: a failure to create a shortcut reports itself and changes
nothing else.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DesktopEntry", "install_shortcut", "shortcut_location"]


@dataclass(frozen=True)
class DesktopEntry:
    """Where a shortcut went, and what it will run."""

    path: Path
    target: str
    repo: Path
    created: bool
    note: str = ""


def _launcher_command(repo: Path, port: int) -> list[str]:
    """The command a shortcut runs: this interpreter, this repository.

    `sys.executable` rather than a bare `forge`, because a shortcut created
    inside a virtual environment must still work after the shell that made it
    is gone - and PATH is not something a desktop launcher inherits.
    """
    return [sys.executable, "-m", "forge.cli", "studio", "--port", str(port)]


def shortcut_location(name: str = "FORGE Studio") -> Path:
    """Where this platform expects an application shortcut to live."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        return base / "Microsoft/Windows/Start Menu/Programs" / f"{name}.lnk"
    if sys.platform == "darwin":
        return Path.home() / "Applications" / f"{name}.command"
    return Path.home() / ".local/share/applications" / "forge-studio.desktop"


def install_shortcut(
    repo: Path, *, port: int = 8080, name: str = "FORGE Studio"
) -> DesktopEntry:
    """Create a launcher for `repo`. Never raises: it reports instead."""
    repo = Path(repo).resolve()
    target = shortcut_location(name)
    command = _launcher_command(repo, port)
    printable = " ".join(command)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            note = _windows_shortcut(target, command, repo)
        elif sys.platform == "darwin":
            note = _macos_command(target, command, repo)
        else:
            note = _linux_desktop(target, command, repo, name)
    except Exception as exc:  # best effort by design: report, never raise
        return DesktopEntry(target, printable, repo, created=False, note=str(exc))

    return DesktopEntry(target, printable, repo, created=True, note=note)


def _windows_shortcut(target: Path, command: list[str], repo: Path) -> str:
    """Write a .lnk via PowerShell's WScript.Shell.

    A .lnk is a binary format; shelling out to the shell object that owns it is
    considerably more honest than hand-assembling one.
    """
    icon = Path(sys.executable).with_name("python.exe")
    args = " ".join(f'"{c}"' if " " in c else c for c in command[1:])
    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{target}');"
        f"$s.TargetPath = '{sys.executable}'; $s.Arguments = '{args}';"
        f"$s.WorkingDirectory = '{repo}'; $s.IconLocation = '{icon}';"
        "$s.Description = 'FORGE Studio'; $s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True, capture_output=True, text=True,
    )
    return "Start Menu shortcut"


def _macos_command(target: Path, command: list[str], repo: Path) -> str:
    joined = " ".join(_quote(c) for c in command)
    body = f"#!/bin/sh\ncd {_quote(str(repo))}\nexec {joined}\n"
    target.write_text(body, encoding="utf-8")
    target.chmod(0o755)
    return "double-clickable launcher in ~/Applications"


def _linux_desktop(target: Path, command: list[str], repo: Path, name: str) -> str:
    body = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        "Comment=A coding agent whose work you read before you merge it\n"
        f"Exec={' '.join(_quote(c) for c in command)}\n"
        f"Path={repo}\n"
        "Terminal=false\n"
        "Categories=Development;IDE;\n"
    )
    target.write_text(body, encoding="utf-8")
    target.chmod(0o755)
    if shutil.which("update-desktop-database"):
        subprocess.run(
            ["update-desktop-database", str(target.parent)],
            check=False, capture_output=True,
        )
    return "application entry in ~/.local/share/applications"


def _quote(value: str) -> str:
    return f'"{value}"' if " " in value else value
