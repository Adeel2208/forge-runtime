"""Target adapters: one interface, many drivers.

The same case set must run against an in-process runtime, a deployed HTTP
service, or a CLI binary, with a config change and no edit to the cases. That
is the difference between a harness and a script that happens to loop.

Every adapter returns the same `Observation`, so graders never learn which
driver produced it. An adapter's job is to translate, and to be honest about
*why* it failed - `TargetUnavailable` and a wrong answer are different facts,
and only the adapter is in a position to tell them apart.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from forge.eval.cases import Case

__all__ = [
    "CallableTarget",
    "CliTarget",
    "HttpTarget",
    "InProcessTarget",
    "Observation",
    "Target",
    "TargetUnavailable",
]


class TargetUnavailable(RuntimeError):
    """The system under test could not be reached.

    Distinct from a wrong answer on purpose: this is an environment fact and
    is the only class of failure the runner may retry.
    """


@dataclass
class Observation:
    """What a target produced, normalised across drivers."""

    answer: str | None = None
    status: str = "UNKNOWN"
    steps: int = 0
    tokens: int = 0
    usd: float = 0.0
    duration_ms: int = 0
    duplicate_effects: int = 0
    run_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    """Trajectory, when the driver can supply it. Trajectory-level graders
    require this; drivers that cannot provide it make those graders skip
    rather than silently pass."""

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def tools_used(self) -> list[str]:
        return [
            str(e.get("payload", {}).get("tool"))
            for e in self.events
            if e.get("type") == "ACTION_DISPATCHED"
        ]

    @property
    def denials(self) -> list[dict[str, Any]]:
        return [
            e.get("payload", {})
            for e in self.events
            if e.get("type") == "POLICY_DECIDED"
            and e.get("payload", {}).get("decision") == "DENY"
        ]

    @property
    def has_trajectory(self) -> bool:
        return bool(self.events)


@runtime_checkable
class Target(Protocol):
    """A system under test."""

    name: str

    @property
    def version(self) -> str:
        """Recorded with every result: a verdict is (case-set x target) version."""
        ...

    async def available(self) -> bool: ...

    async def setup(self) -> None:
        """Idempotent. Called once per run, before any case."""
        ...

    async def execute(self, case: Case, *, seed: int) -> Observation: ...

    async def teardown(self) -> None:
        """Idempotent. Must run even when cases failed."""
        ...


# ─────────────────────────────────────────────────────────────── in-process


class InProcessTarget:
    """Drives the FORGE runtime directly, in this process.

    Each case gets its own event store in a scratch directory, so no state
    leaks between cases. Fixtures make the model deterministic; without one
    the case runs against whatever provider the deployment is configured with.
    """

    name = "inprocess"

    def __init__(
        self,
        *,
        config: Any = None,
        fixtures_dir: str | Path = "cases/fixtures",
        workdir: str | Path | None = None,
    ) -> None:
        from forge import __version__

        self._config = config
        self._version = f"forge/{__version__}"
        self._model_label = ""
        self._fixtures = Path(fixtures_dir)
        self._workdir = Path(workdir) if workdir else None
        self._owns_workdir = workdir is None

    @property
    def version(self) -> str:
        """Runtime version *and* model.

        A verdict is only interpretable as (case-set version x target
        version), and for an agent the model is part of the target: the same
        cases against the same code with a different model is a different
        system, and a diff that hid that would be misleading.
        """
        return f"{self._version}{self._model_label}"

    async def available(self) -> bool:
        return True

    async def setup(self) -> None:
        if self._workdir is None:
            self._workdir = Path(tempfile.mkdtemp(prefix="forge-eval-"))
        self._workdir.mkdir(parents=True, exist_ok=True)

        from forge.config import ForgeConfig

        config = self._config if self._config is not None else ForgeConfig.load()
        if config.providers:
            first = config.providers[0]
            self._model_label = f" {first.kind}/{first.model}"

    async def teardown(self) -> None:
        if self._owns_workdir and self._workdir and self._workdir.exists():
            shutil.rmtree(self._workdir, ignore_errors=True)
        self._workdir = None

    async def execute(self, case: Case, *, seed: int) -> Observation:
        import time

        from forge.config import ForgeConfig
        from forge.deployment import Forge
        from forge.evaluation.faults import FaultClass, FaultInjector, FaultSpec
        from forge.llm.mock import MockProvider
        from forge.tools.builtin import WORKSPACE, set_flakiness

        if self._workdir is None:
            raise TargetUnavailable("target not set up")

        # Hermetic per case: fresh database, fresh module-level tool state.
        WORKSPACE.clear()
        set_flakiness(0)
        db = self._workdir / f"{case.id.replace('/', '_')}.db"
        if db.exists():
            db.unlink()

        # Evaluate the *project's* configuration - its model, its tools, its
        # policy. Falling back to library defaults would silently grade a mock
        # provider and bundled example tools, so every case would be measuring
        # something nobody deployed.
        base = self._config if self._config is not None else ForgeConfig.load()
        config = replace(
            base, database_url=f"sqlite:///{db}", seed=seed
        )

        providers = None
        if case.fixture:
            providers = [MockProvider.from_fixture(self._fixtures / case.fixture)]

        injector = None
        if case.faults:
            injector = FaultInjector(
                specs=[
                    FaultSpec(kind=FaultClass(f), at_step=None) for f in case.faults
                ],
                seed=seed,
            )

        started = time.monotonic()
        async with Forge(config=config, providers=providers) as forge:
            runtime = forge._build_runtime()
            if injector is not None:
                runtime.faults = injector
            from forge.core.contracts import TaskSpec

            task = TaskSpec(
                goal=case.goal,
                tools=list(case.tools),
                max_steps=case.max_steps or config.budget.max_steps,
            )
            try:
                result = await runtime.start(task)
            except BaseException as exc:
                # A crashed worker is a legitimate observation for resilience
                # cases, not an infrastructure failure. Resume and report.
                if type(exc).__name__ != "SimulatedCrash":
                    raise
                runs = await forge.runs(limit=1)
                run_id = str(runs[0]["run_id"]) if runs else ""
                fresh = forge._build_runtime()
                result = await fresh.resume(run_id)

            events = await forge.events(result.run_id)

        return Observation(
            answer=result.answer,
            status=result.status.value,
            steps=result.steps,
            tokens=result.usage.total_tokens,
            usd=result.usage.usd,
            duration_ms=int((time.monotonic() - started) * 1000),
            duplicate_effects=result.duplicate_effects,
            run_id=result.run_id,
            events=[
                {
                    "seq": e.seq,
                    "type": e.type.value,
                    "step_index": e.step_index,
                    "payload": e.payload,
                }
                for e in events
            ],
            raw=result.to_dict(),
        )


# ────────────────────────────────────────────────────────────────────── HTTP


class HttpTarget:
    """Drives a deployed FORGE service over its HTTP API.

    The same case set that runs in-process runs against staging with a config
    change - which is the whole point of the adapter layer.
    """

    name = "http"

    def __init__(
        self,
        base_url: str,
        *,
        poll_interval_s: float = 0.5,
        api_key: str | None = None,
        version: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.poll_interval_s = poll_interval_s
        self._api_key = api_key
        self._version = version or "unknown"
        self._client: Any = None

    @property
    def version(self) -> str:
        return f"http/{self._version}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def setup(self) -> None:
        import httpx

        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        try:
            resp = await self._client.get("/livez", headers=self._headers())
            if resp.status_code == 200:
                self._version = str(resp.json().get("version", self._version))
        except Exception as exc:
            raise TargetUnavailable(f"cannot reach {self.base_url}: {exc}") from exc

    async def teardown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def available(self) -> bool:
        if self._client is None:
            return False
        try:
            resp = await self._client.get("/livez", headers=self._headers())
            return bool(resp.status_code == 200)
        except Exception:
            return False

    async def execute(self, case: Case, *, seed: int) -> Observation:
        import time

        import httpx

        del seed  # a remote target owns its own determinism
        if self._client is None:
            raise TargetUnavailable("target not set up")

        payload: dict[str, Any] = {"goal": case.goal, "tools": list(case.tools)}
        if case.max_steps:
            payload["max_steps"] = case.max_steps

        started = time.monotonic()
        try:
            accepted = await self._client.post("/runs", json=payload, headers=self._headers())
            if accepted.status_code >= 500:
                raise TargetUnavailable(f"service returned {accepted.status_code}")
            accepted.raise_for_status()
            run_id = accepted.json()["run_id"]

            deadline = time.monotonic() + case.timeout_s
            view: dict[str, Any] = {}
            while time.monotonic() < deadline:
                await asyncio.sleep(self.poll_interval_s)
                resp = await self._client.get(f"/runs/{run_id}", headers=self._headers())
                if resp.status_code == 404:
                    continue
                view = resp.json()
                if view.get("status") in ("COMPLETED", "FAILED", "ABORTED"):
                    break

            events_resp = await self._client.get(
                f"/runs/{run_id}/events", headers=self._headers()
            )
            events = events_resp.json() if events_resp.status_code == 200 else []
        except httpx.HTTPError as exc:
            raise TargetUnavailable(f"transport failure: {exc}") from exc

        return Observation(
            answer=view.get("answer"),
            status=str(view.get("status", "UNKNOWN")),
            steps=int(view.get("steps", 0)),
            tokens=int(view.get("tokens", 0)),
            usd=float(view.get("usd", 0.0)),
            duration_ms=int((time.monotonic() - started) * 1000),
            duplicate_effects=int(view.get("duplicate_effects", 0)),
            run_id=run_id,
            events=events,
            raw=view,
        )


# ─────────────────────────────────────────────────────────────────────── CLI


class CliTarget:
    """Drives a CLI binary as a subprocess.

    Useful for testing the shipped artifact rather than the importable one -
    packaging bugs only show up through this driver.
    """

    name = "cli"

    def __init__(
        self,
        command: list[str] | None = None,
        *,
        workdir: str | Path | None = None,
        version: str = "cli",
    ) -> None:
        self.command = command or [sys.executable, "-m", "forge.cli"]
        self._workdir = Path(workdir) if workdir else None
        self._owns_workdir = workdir is None
        self._version = version

    @property
    def version(self) -> str:
        return self._version

    async def available(self) -> bool:
        return True

    async def setup(self) -> None:
        if self._workdir is None:
            self._workdir = Path(tempfile.mkdtemp(prefix="forge-cli-eval-"))
        self._workdir.mkdir(parents=True, exist_ok=True)

    async def teardown(self) -> None:
        if self._owns_workdir and self._workdir and self._workdir.exists():
            shutil.rmtree(self._workdir, ignore_errors=True)
        self._workdir = None

    async def execute(self, case: Case, *, seed: int) -> Observation:
        import time

        if self._workdir is None:
            raise TargetUnavailable("target not set up")

        db = self._workdir / f"{case.id.replace('/', '_')}.db"
        argv = [
            *self.command, "run", case.goal,
            "--db", str(db), "--json",
            "--tools", ",".join(case.tools),
        ]
        started = time.monotonic()
        try:
            proc = await asyncio.to_thread(
                subprocess.run, argv, capture_output=True, text=True,
                timeout=case.timeout_s,
            )
        except FileNotFoundError as exc:
            raise TargetUnavailable(f"cannot execute {self.command[0]!r}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"cli exceeded {case.timeout_s}s") from exc

        del seed
        try:
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise TargetUnavailable(
                f"cli produced no parseable result (exit {proc.returncode}): "
                f"{proc.stderr[:200]}"
            ) from exc

        return Observation(
            answer=payload.get("answer"),
            status=str(payload.get("status", "UNKNOWN")),
            steps=int(payload.get("steps", 0)),
            tokens=int((payload.get("usage") or {}).get("input_tokens", 0))
            + int((payload.get("usage") or {}).get("output_tokens", 0)),
            usd=float((payload.get("usage") or {}).get("usd", 0.0)),
            duration_ms=int((time.monotonic() - started) * 1000),
            duplicate_effects=int(payload.get("duplicate_effects", 0)),
            run_id=payload.get("run_id"),
            raw=payload,
        )


# ──────────────────────────────────────────────────────────────── callable


class CallableTarget:
    """Wraps an arbitrary async function. The escape hatch for anything else -
    a competitor's agent, a stub, or a deliberately broken build used to prove
    the harness actually fails (see `tests/eval/`)."""

    def __init__(self, fn: Any, *, name: str = "callable", version: str = "0") -> None:
        self.name = name
        self._fn = fn
        self._version = version

    @property
    def version(self) -> str:
        return self._version

    async def available(self) -> bool:
        return True

    async def setup(self) -> None:
        return None

    async def teardown(self) -> None:
        return None

    async def execute(self, case: Case, *, seed: int) -> Observation:
        result = await self._fn(case, seed)
        return result if isinstance(result, Observation) else Observation(answer=str(result))
