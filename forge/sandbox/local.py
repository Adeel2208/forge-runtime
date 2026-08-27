"""The local sandbox: a confined child process.

Available everywhere, and cheap - the overhead over a plain `subprocess.run`
is a few syscalls, so bounding a command costs essentially nothing. That
matters: a sandbox people disable because it is slow protects nobody.

What it enforces:

* **No shell.** `argv` is passed as a list, `shell=False` always. There is no
  string for `;`, `&&`, `$(...)` or a quote to break out of.
* **A scrubbed environment.** Allow-listed variables only, so tokens and keys
  are simply absent.
* **A confined working directory**, which must be inside the workspace.
* **Wall-clock and output ceilings**, so a hung or chatty command cannot
  wedge the runtime or exhaust memory through a pipe.
* **OS-enforced resource caps** where the platform provides them - a Windows
  Job Object, or Unix rlimits - covering memory and process count.
* **Guaranteed process-tree kill.** A command that spawns children cannot
  leave orphans behind after a timeout.

What it does *not* enforce, stated plainly because the distinction is the
whole point: no filesystem isolation, no network isolation, no privilege
separation. It bounds accidents and runaway resource use. It does not contain
a program that is actively trying to escape - that needs `CONTAINER`.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from forge.sandbox.base import (
    Isolation,
    Sandbox,
    SandboxError,
    SandboxResult,
    SandboxSpec,
    scrub_environment,
)
from forge.telemetry.logging import get_logger

__all__ = ["LocalSandbox"]

log = get_logger("forge.sandbox.local")

_IS_WINDOWS = sys.platform == "win32"


class LocalSandbox:
    """Runs a command in a resource-capped child process."""

    name = "local"

    def __init__(self, *, workspace_root: Path | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self._limiter = _JobObjectLimiter() if _IS_WINDOWS else _RlimitLimiter()

    @property
    def isolation(self) -> Isolation:
        return Isolation.CONFINED

    async def available(self) -> bool:
        return True

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "isolation": self.isolation.label,
            "enforces": [
                "no shell interpretation",
                "scrubbed environment",
                "confined working directory",
                "wall-clock timeout",
                "output size ceiling",
                "process-tree kill",
                *self._limiter.enforces(),
            ],
            "does_not_enforce": [
                "filesystem isolation",
                "network isolation",
                "privilege separation",
            ],
            "backend": self._limiter.name,
        }

    # -- execution ---------------------------------------------------------

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        # Everything blocking - path resolution included - happens on the
        # worker thread, so the event loop is never held by a stat.
        return await asyncio.to_thread(self._run_sync, spec)

    def _resolve_cwd(self, spec: SandboxSpec) -> Path:
        """Resolve and confine the working directory, before anything spawns."""
        cwd = Path(spec.cwd).resolve()
        if self.workspace_root is not None:
            try:
                cwd.relative_to(self.workspace_root)
            except ValueError as exc:
                raise SandboxError(
                    f"working directory {cwd} is outside the workspace "
                    f"{self.workspace_root}"
                ) from exc
        if not cwd.is_dir():
            raise SandboxError(f"working directory does not exist: {cwd}")
        return cwd

    def _run_sync(self, spec: SandboxSpec) -> SandboxResult:
        cwd = self._resolve_cwd(spec)
        env = scrub_environment(spec.env_passthrough)
        limits = spec.limits
        limits_hit: list[str] = []
        started = time.monotonic()

        popen_kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,   # never wait on input
            "text": True,
            "errors": "replace",
            "shell": False,                # not negotiable
        }
        # A new process group / session is what makes tree-kill possible.
        if _IS_WINDOWS:
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED_IF_NEEDED
            )
        else:
            popen_kwargs["start_new_session"] = True
            popen_kwargs["preexec_fn"] = self._limiter.preexec(limits)

        try:
            proc = subprocess.Popen(spec.argv, **popen_kwargs)
        except FileNotFoundError:
            return SandboxResult(
                exit_code=127,
                stderr=f"command not found: {spec.argv[0]}",
                isolation=self.isolation,
            )
        except OSError as exc:
            raise SandboxError(f"could not start {spec.argv[0]!r}: {exc}") from exc

        # Apply OS limits to the live process, then let it run.
        applied = self._limiter.apply(proc, limits)
        limits_hit.extend(applied.get("warnings", []))

        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=limits.wall_clock_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            limits_hit.append(f"wall_clock_s={limits.wall_clock_s}")
            _kill_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - very stubborn
                stdout, stderr = "", ""
        finally:
            self._limiter.release()

        truncated = False
        cap = limits.max_output_bytes
        if len(stdout) > cap:
            stdout, truncated = stdout[:cap], True
        if len(stderr) > cap:
            stderr, truncated = stderr[:cap], True
        if truncated:
            limits_hit.append(f"max_output_bytes={cap}")

        if limits.network:
            limits_hit.append("network=requested but not enforceable below CONTAINER")

        return SandboxResult(
            exit_code=124 if timed_out else (proc.returncode or 0),
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            truncated=truncated,
            isolation=self.isolation,
            limits_hit=limits_hit,
        )


# Windows needs no suspend for our purposes; kept as a named zero so the
# creationflags expression above reads clearly.
_CREATE_SUSPENDED_IF_NEEDED = 0


def _kill_tree(proc: subprocess.Popen[Any]) -> None:
    """Kill the process and everything it spawned.

    Killing only the direct child leaves orphaned grandchildren holding CPU,
    memory and file locks - which is exactly the runaway the timeout existed
    to stop.
    """
    try:
        import psutil

        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for child in children:
            with _suppress():
                child.kill()
        with _suppress():
            parent.kill()
        psutil.wait_procs([parent, *children], timeout=5)
        return
    except ImportError:
        pass
    except Exception as exc:
        log.warning("psutil tree-kill failed", error=str(exc))

    # No psutil: use the OS group primitives directly.
    with _suppress():
        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10, check=False,
            )
        else:
            # Unix-only, guarded by the branch above; typeshed hides
            # these when checking against a Windows platform.
            os.killpg(os.getpgid(proc.pid), 9)  # type: ignore[attr-defined]
    with _suppress():
        proc.kill()


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return True


# ── platform limiters ────────────────────────────────────────────────────


class _RlimitLimiter:
    """Unix: rlimits applied in the child before exec."""

    name = "rlimit"

    def enforces(self) -> list[str]:
        try:
            import resource  # noqa: F401
        except ImportError:  # pragma: no cover - non-Unix
            return []
        return ["address-space cap (RLIMIT_AS)", "process cap (RLIMIT_NPROC)"]

    def preexec(self, limits: Any) -> Any:
        try:
            import resource
        except ImportError:  # pragma: no cover - non-Unix
            return None

        # `resource` is Unix-only, and this file is type-checked on Windows
        # where typeshed does not expose these names. The import above is the
        # real guard; these ignores are about the checker's platform, not
        # about the code being unsafe.
        def apply() -> None:  # pragma: no cover - runs in the forked child
            if limits.memory_mb:
                cap = limits.memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (cap, cap))  # type: ignore[attr-defined]
            if limits.max_processes:
                resource.setrlimit(  # type: ignore[attr-defined]
                    resource.RLIMIT_NPROC,  # type: ignore[attr-defined]
                    (limits.max_processes, limits.max_processes),
                )
            if limits.cpu_seconds:
                resource.setrlimit(  # type: ignore[attr-defined]
                    resource.RLIMIT_CPU,  # type: ignore[attr-defined]
                    (limits.cpu_seconds, limits.cpu_seconds),
                )

        return apply

    def apply(self, proc: Any, limits: Any) -> dict[str, Any]:
        del proc, limits
        return {}  # already applied in the child

    def release(self) -> None:
        return None


class _JobObjectLimiter:
    """Windows: a Job Object holding the process and its descendants.

    Job Objects are the real thing on Windows - the kernel enforces the caps,
    and `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` guarantees the whole tree dies
    with the job even if the runtime crashes.
    """

    name = "job-object"

    def __init__(self) -> None:
        self._job: Any = None

    def enforces(self) -> list[str]:
        if not self._win32_available():
            return []
        return [
            "job-object memory cap",
            "job-object active-process cap",
            "kill-on-close (tree dies with the job)",
        ]

    @staticmethod
    def _win32_available() -> bool:
        try:
            import win32job  # noqa: F401
        except ImportError:
            return False
        return True

    def preexec(self, limits: Any) -> Any:
        del limits
        return None  # not a Unix concept

    def apply(self, proc: Any, limits: Any) -> dict[str, Any]:
        if not self._win32_available():
            return {"warnings": ["memory/process caps unavailable (pywin32 not installed)"]}

        try:
            import win32api
            import win32con
            import win32job

            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation
            )
            basic = info["BasicLimitInformation"]
            flags = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

            if limits.memory_mb:
                info["ProcessMemoryLimit"] = limits.memory_mb * 1024 * 1024
                flags |= win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            if limits.max_processes:
                basic["ActiveProcessLimit"] = limits.max_processes
                flags |= win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS

            basic["LimitFlags"] = flags
            info["BasicLimitInformation"] = basic
            win32job.SetInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation, info
            )

            handle = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, proc.pid
            )
            win32job.AssignProcessToJobObject(job, handle)
            self._job = job
            return {}
        except Exception as exc:
            log.warning("job object not applied", error=f"{type(exc).__name__}: {exc}")
            return {"warnings": [f"job-object limits not applied: {exc}"]}

    def release(self) -> None:
        """Close the job. Kill-on-close takes the whole tree with it."""
        if self._job is None:
            return
        try:
            import win32api

            win32api.CloseHandle(self._job)
        except Exception:
            pass
        finally:
            self._job = None


def build_local_sandbox(workspace_root: Path | None = None) -> Sandbox:
    return LocalSandbox(workspace_root=workspace_root)
