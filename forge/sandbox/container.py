"""The container sandbox: the first tier that contains hostile code.

Everything below `CONTAINER` bounds accidents. This tier bounds intent -
filesystem, network, PID and user namespaces, so a command that wants to read
your home directory or phone out simply cannot.

Uses the `docker` CLI rather than a Python SDK: it is what people already have
and already understand, it keeps the dependency list at four packages
(ADR-0002), and `docker run` flags are the same thing an operator would type
when reproducing what the agent did.

Cost: roughly 200-600ms of container start per command. That is the honest
price of real isolation, and it is why the tier is selected rather than
assumed - `forge doctor` reports which tier is active and what it costs.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from forge.sandbox.base import (
    Isolation,
    SandboxError,
    SandboxResult,
    SandboxSpec,
    scrub_environment,
)
from forge.telemetry.logging import get_logger

__all__ = ["DEFAULT_IMAGE", "ContainerSandbox"]

log = get_logger("forge.sandbox.container")

DEFAULT_IMAGE = "python:3.12-slim"


class ContainerSandbox:
    """Runs a command inside a throwaway container."""

    name = "container"

    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        workspace_root: Path | None = None,
        runtime: str = "docker",
        mount_readonly: bool = False,
    ) -> None:
        self.image = image
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self.runtime = runtime
        self.mount_readonly = mount_readonly
        self._checked: bool | None = None

    @property
    def isolation(self) -> Isolation:
        return Isolation.CONTAINER

    async def available(self) -> bool:
        if self._checked is not None:
            return self._checked
        if shutil.which(self.runtime) is None:
            self._checked = False
            return False
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [self.runtime, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, timeout=15,
            )
            self._checked = proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            self._checked = False
        return self._checked

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "isolation": self.isolation.label,
            "image": self.image,
            "enforces": [
                "filesystem isolation (only the workspace is mounted)",
                "network isolation (--network none)",
                "PID namespace",
                "non-root user",
                "read-only root filesystem",
                "dropped capabilities, no privilege escalation",
                "memory and CPU limits",
                "process cap",
                "wall-clock timeout and tree-kill (--rm)",
            ],
            "does_not_enforce": [
                "kernel-level exploits (shares the host kernel)",
            ],
            "cost": "~200-600ms container start per command",
        }

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        if not await self.available():
            raise SandboxError(
                f"{self.runtime} is not available; cannot provide CONTAINER isolation"
            )
        return await asyncio.to_thread(self._run_sync, spec)

    def _run_sync(self, spec: SandboxSpec) -> SandboxResult:
        cwd = Path(spec.cwd).resolve()
        mount_root = self.workspace_root or cwd
        try:
            relative = cwd.relative_to(mount_root)
        except ValueError as exc:
            raise SandboxError(
                f"working directory {cwd} is outside the mounted workspace {mount_root}"
            ) from exc

        limits = spec.limits
        workdir = "/workspace" + ("" if str(relative) == "." else f"/{relative.as_posix()}")

        argv = [
            self.runtime, "run", "--rm", "--interactive=false",
            # Isolation flags. Each one closes a specific escape.
            "--network", "bridge" if limits.network else "none",
            "--user", "1000:1000",              # never root inside
            "--read-only",                       # root fs immutable...
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",   # ...except scratch
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(limits.max_processes),
            "--memory", f"{limits.memory_mb}m",
            "--memory-swap", f"{limits.memory_mb}m",       # no swap escape hatch
            "--cpus", "2",
            "--workdir", workdir,
            "--mount",
            f"type=bind,source={mount_root},target=/workspace"
            + (",readonly" if self.mount_readonly else ""),
        ]
        for key, value in scrub_environment(spec.env_passthrough).items():
            argv += ["--env", f"{key}={value}"]
        argv += [self.image, *spec.argv]

        started = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, errors="replace",
                timeout=limits.wall_clock_s + 30,   # allow for image pull/start
                check=False,
            )
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired:
            timed_out, stdout, stderr, code = True, "", "container timed out", 124

        truncated = False
        cap = limits.max_output_bytes
        if len(stdout) > cap:
            stdout, truncated = stdout[:cap], True
        if len(stderr) > cap:
            stderr, truncated = stderr[:cap], True

        limits_hit: list[str] = []
        if timed_out:
            limits_hit.append(f"wall_clock_s={limits.wall_clock_s}")
        if truncated:
            limits_hit.append(f"max_output_bytes={cap}")
        if code == 137:
            limits_hit.append(f"memory_mb={limits.memory_mb} (OOM-killed)")

        return SandboxResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            truncated=truncated,
            isolation=self.isolation,
            limits_hit=limits_hit,
        )
