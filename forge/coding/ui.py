"""Terminal rendering, with no rendering dependency.

Four packages is the whole dependency list (ADR-0002), and a terminal UI is
not worth breaking that for. This is ANSI escape codes behind named functions,
which degrade to plain text whenever colour would be wrong: a redirected
stream, `NO_COLOR`, a dumb terminal, or a Windows console that refuses VT
processing.

Colour carries meaning here and is never decorative:

    accent   the agent is doing something
    ok       it worked
    warn     a policy refusal or a retry - notable, not fatal
    error    the run failed
    dim      context you can skip when scanning
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

__all__ = [
    "accent",
    "bold",
    "clear_line",
    "dim",
    "error",
    "field",
    "header",
    "ok",
    "rule",
    "supports_colour",
    "warn",
]


def _enable_windows_vt() -> bool:
    """Turn on ANSI processing in a Windows console. Returns whether it worked."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # -11 is STD_OUTPUT_HANDLE; 0x0004 is ENABLE_VIRTUAL_TERMINAL_PROCESSING.
        return bool(kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7))
    except Exception:
        return False


def supports_colour(stream: TextIO | None = None) -> bool:
    target = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(target, "isatty") or not target.isatty():
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return _enable_windows_vt()


_ENABLED = supports_colour()

_ACCENT = "\033[36m"
_OK = "\033[32m"
_WARN = "\033[33m"
_ERROR = "\033[31m"
_DIM = "\033[90m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _wrap(text: str, code: str) -> str:
    return f"{code}{text}{_RESET}" if _ENABLED else text


def accent(text: str) -> str:
    return _wrap(text, _ACCENT)


def ok(text: str) -> str:
    return _wrap(text, _OK)


def warn(text: str) -> str:
    return _wrap(text, _WARN)


def error(text: str) -> str:
    return _wrap(text, _ERROR)


def dim(text: str) -> str:
    return _wrap(text, _DIM)


def bold(text: str) -> str:
    return _wrap(text, _BOLD)


def rule(width: int = 66) -> str:
    """A horizontal rule.

    ASCII on purpose: a Windows console is cp1252 by default, where box-drawing
    characters raise UnicodeEncodeError part-way through a line.
    """
    return dim("-" * width)


def header(title: str, subtitle: str = "") -> str:
    line = bold(accent(title))
    return f"{line}  {dim(subtitle)}" if subtitle else line


def field(label: str, value: str, *, width: int = 14) -> str:
    return f"  {dim(label.ljust(width))}{value}"


def clear_line() -> str:
    """Erase the current line, for in-place progress updates."""
    return "\r\033[2K" if _ENABLED else "\r"
