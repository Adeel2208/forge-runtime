"""Sandbox contracts, and an honest account of what each tier guarantees.

Sandboxing is where security claims get overstated, so the isolation level is
a first-class value that a sandbox *declares* and policy can *require*. You
cannot accidentally grant shell execution under weak confinement: the
capability names a minimum tier, and if the available sandbox is below it the
capability stays denied, with a reason that says why.

The tiers, stated as what they actually enforce:

    NONE       In-process. No isolation of any kind. The tool runs with the
               runtime's own privileges, in its address space.

    CONFINED   A child process. No shell interpretation, a scrubbed
               environment, a confined working directory, wall-clock and
               output ceilings, and OS-enforced resource caps where the
               platform provides them (Job Objects on Windows, rlimits on
               Unix). Guaranteed process-tree kill.

               Does NOT provide: filesystem isolation, network isolation, or
               privilege separation. A determined command can still read your
               home directory. This bounds *accidents and runaway resource
               use*, not a hostile program.

    CONTAINER  A container. Filesystem, network, PID and user namespaces,
               plus memory and CPU limits. This is the first tier that
               contains something actively trying to escape.

Anything that claims more than it enforces is worse than no sandbox, because
people act on the claim.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "SAFE_ENV_KEYS",
    "Isolation",
    "Sandbox",
    "SandboxError",
    "SandboxLimits",
    "SandboxResult",
    "SandboxSpec",
    "scrub_environment",
]


class SandboxError(RuntimeError):
    """The sandbox could not run the command. Not the command's own failure."""


class Isolation(IntEnum):
    """Ordered, so policy can require a *minimum* tier with a comparison."""

    NONE = 0
    CONFINED = 1
    CONTAINER = 2

    @property
    def label(self) -> str:
        return self.name.lower()

    @property
    def contains_hostile_code(self) -> bool:
        """True only where an actively malicious program is contained.

        `CONFINED` bounds resource use and scrubs secrets; it does not stop a
        program that wants to read your home directory. Saying so in one
        property keeps every caller honest.
        """
        return self >= Isolation.CONTAINER

    def describe(self) -> str:
        return {
            Isolation.NONE: "no isolation - runs in the runtime process",
            Isolation.CONFINED: (
                "child process: no shell, scrubbed env, confined cwd, "
                "resource caps, tree-kill. No filesystem or network isolation."
            ),
            Isolation.CONTAINER: (
                "container: filesystem, network, PID and user isolation, "
                "plus memory and CPU limits."
            ),
        }[self]


@dataclass(frozen=True)
class SandboxLimits:
    """Resource ceilings. Defaults are deliberately small.

    These exist so a runaway command cannot degrade the machine the runtime is
    running on - the ceiling costs nothing to set and bounds the blast radius
    of an infinite loop or a memory leak.
    """

    wall_clock_s: float = 120.0
    memory_mb: int = 2048
    max_processes: int = 64
    max_output_bytes: int = 1_000_000
    cpu_seconds: int = 0
    """0 means unbounded CPU time; wall clock still applies."""

    network: bool = False
    """Only enforceable at CONTAINER. Ignored, and reported as such, below it."""


@dataclass(frozen=True)
class SandboxSpec:
    """One command to execute."""

    argv: tuple[str, ...]
    cwd: Path
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    env_passthrough: tuple[str, ...] = ()
    """Extra environment variable names to preserve, beyond the safe defaults."""

    def __post_init__(self) -> None:
        if not self.argv:
            raise SandboxError("empty command")
        if any(not isinstance(a, str) for a in self.argv):
            raise SandboxError("argv must be strings")


@dataclass
class SandboxResult:
    """What happened. `isolation` records the tier that actually ran it."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    truncated: bool = False
    isolation: Isolation = Isolation.NONE
    limits_hit: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def output(self) -> str:
        parts = [p for p in (self.stdout, self.stderr) if p]
        return "\n".join(parts)


@runtime_checkable
class Sandbox(Protocol):
    """Executes a command under some isolation tier."""

    name: str

    @property
    def isolation(self) -> Isolation: ...

    async def available(self) -> bool:
        """Whether this sandbox can run here, right now."""
        ...

    async def run(self, spec: SandboxSpec) -> SandboxResult: ...

    def describe(self) -> dict[str, object]:
        """What this sandbox enforces. Surfaced by `forge doctor`."""
        ...


# ── environment scrubbing ────────────────────────────────────────────────
#
# Secrets reach subprocesses through the environment more often than through
# any other channel, and a command that never sees a token cannot exfiltrate,
# log, or commit one. The list is an allow-list because a deny-list of secret
# names is a game you lose the first time someone invents a new prefix.

SAFE_ENV_KEYS: frozenset[str] = frozenset({
    # Needed for almost anything to run at all.
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "TZ",
    # Windows equivalents.
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS", "APPDATA", "LOCALAPPDATA",
    # Toolchain behaviour, not credentials.
    "PYTHONPATH", "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE",
    "VIRTUAL_ENV", "CONDA_PREFIX", "NODE_PATH", "GOPATH", "GOROOT",
    "CARGO_HOME", "RUSTUP_HOME", "JAVA_HOME",
})

# Belt and braces: even if an allow-listed name somehow carries a secret,
# these patterns are dropped.
_SECRET_PATTERN = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|APIKEY|API_KEY|CREDENTIAL|PRIVATE"
    r"|_KEY$|^KEY$|AUTH|SESSION|COOKIE)",
    re.IGNORECASE,
)


def scrub_environment(
    passthrough: tuple[str, ...] = (), *, base: dict[str, str] | None = None
) -> dict[str, str]:
    """Build a minimal environment for a sandboxed command."""
    source = base if base is not None else dict(os.environ)
    allowed = SAFE_ENV_KEYS | {p.upper() for p in passthrough}

    scrubbed = {
        key: value
        for key, value in source.items()
        if key.upper() in allowed and not _SECRET_PATTERN.search(key)
    }
    # Mark the environment so a program can tell it is sandboxed, and so a
    # human reading a process list can too.
    scrubbed["FORGE_SANDBOX"] = "1"
    return scrubbed
