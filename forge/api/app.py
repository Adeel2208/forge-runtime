"""FastAPI service around a `Forge` deployment.

    uvicorn "forge.api:create_app" --factory --port 8080

Design notes, because they are what separate a service from routes bolted onto
a script:

* **Runs are asynchronous.** `POST /runs` returns `202` with a run id. A
  long-horizon run can take minutes; holding an HTTP connection open for one
  turns a client timeout into an orphaned side effect.
* **In-flight runs are owned.** Every run holds a lease while it executes. If
  this process dies, the lease lapses and a supervisor - here or on another
  replica - reclaims and resumes it. The durability guarantee therefore holds
  at the service boundary, not only inside the runtime.
* **Shutdown drains.** SIGTERM stops accepting work and waits for in-flight
  runs; whatever does not finish in time is left leased-but-expiring, which is
  precisely the state recovery is built for.
* **The event log is the read model.** `GET /runs/{id}/events` streams the
  same durable log the runtime writes, so an observer sees what happened
  rather than a summary someone remembered to update.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from forge import __version__
from forge.api.dashboard import DASHBOARD_HTML
from forge.api.security import ApiKeyAuth, Principal, RateLimiter
from forge.config import ForgeConfig
from forge.core.enums import RunStatus
from forge.deployment import Forge
from forge.ids import new_id
from forge.runtime.supervisor import Supervisor, SupervisorConfig
from forge.state.leases import SQLiteLeaseStore
from forge.state.projection import project
from forge.telemetry.logging import configure_logging, get_logger

__all__ = ["create_app"]

log = get_logger("forge.api")

MAX_GOAL_CHARS = 8_000
MAX_TOOLS = 64


async def _principal(request: Request) -> Principal:
    """Authenticate and rate-limit, reading per-app collaborators off state.

    Defined at module scope deliberately: `from __future__ import annotations`
    turns every annotation into a string, and FastAPI resolves those in the
    module namespace. A dependency alias declared inside `create_app` is not
    visible there, and FastAPI quietly demotes the parameter to a query field
    instead of failing - a 422 that looks like a client error but is ours.
    """
    auth: ApiKeyAuth = request.app.state.auth
    limiter: RateLimiter = request.app.state.limiter
    who = auth.authenticate(request)
    limiter.check(who)
    return who


Caller = Annotated[Principal, Depends(_principal)]


class RunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=MAX_GOAL_CHARS)
    tools: list[str] | None = Field(default=None, max_length=MAX_TOOLS)
    max_steps: int | None = Field(default=None, ge=1, le=200)


class RunAccepted(BaseModel):
    run_id: str
    status: str
    href: str


class RunView(BaseModel):
    run_id: str
    status: str
    answer: str | None = None
    steps: int = 0
    tokens: int = 0
    usd: float = 0.0
    resumed: bool = False
    duplicate_effects: int = 0
    owned_by: str | None = None
    error: str | None = None


def create_app(
    config: ForgeConfig | None = None,
    *,
    deployment: Forge | None = None,
    require_auth: bool = True,
    api_keys: dict[str, str] | None = None,
    rate_limit: int = 60,
    enable_supervisor: bool = True,
    configure_logs: bool = True,
) -> FastAPI:
    """Build the ASGI app. Collaborators are injectable so it is testable."""
    if configure_logs:
        configure_logging()

    resolved = config or ForgeConfig.load()
    auth = ApiKeyAuth(api_keys, required=require_auth)
    limiter = RateLimiter(limit=rate_limit)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await app.state.forge.open()
        if app.state.supervisor is not None:
            await app.state.leases.open()
            await app.state.supervisor.start()
        log.info(
            "service started",
            version=__version__,
            auth="enabled" if auth.enabled else "disabled",
            supervisor=app.state.supervisor is not None,
        )
        try:
            yield
        finally:
            app.state.draining = True
            await _drain(app)
            if app.state.supervisor is not None:
                await app.state.supervisor.stop()
                await app.state.leases.close()
            await app.state.forge.close()
            log.info("service stopped")

    app = FastAPI(
        title="FORGE",
        version=__version__,
        summary="Durable, policy-aware execution runtime for long-horizon AI agents.",
        lifespan=lifespan,
    )

    app.state.forge = deployment or Forge(config=resolved)
    app.state.config = resolved
    app.state.inflight = {}
    app.state.errors = {}
    app.state.draining = False

    if enable_supervisor and resolved.is_sqlite:
        app.state.leases = SQLiteLeaseStore(resolved.sqlite_path)
        app.state.supervisor = Supervisor(
            store=app.state.forge._store,
            leases=app.state.leases,
            resume=lambda run_id: app.state.forge.resume(run_id),
            config=SupervisorConfig(),
        )
    else:
        app.state.leases = None
        app.state.supervisor = None

    app.state.auth = auth
    app.state.limiter = limiter

    def _reject_if_draining() -> None:
        if app.state.draining:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="service is shutting down; retry against another replica",
            )

    # -- runs --------------------------------------------------------------

    @app.post("/runs", status_code=202, response_model=RunAccepted)
    async def start_run(body: RunRequest, who: Caller) -> RunAccepted:
        """Accept a task and execute it under a lease."""
        _reject_if_draining()
        run_id = new_id("run")
        bound = log.bind(run_id=run_id, principal=who.label)
        bound.info("run accepted", goal_chars=len(body.goal))

        async def execute() -> None:
            supervisor: Supervisor | None = app.state.supervisor
            try:
                if supervisor is not None:
                    async with supervisor.owning(run_id):
                        await app.state.forge.run(
                            body.goal, tools=body.tools,
                            max_steps=body.max_steps, run_id=run_id,
                        )
                else:
                    await app.state.forge.run(
                        body.goal, tools=body.tools,
                        max_steps=body.max_steps, run_id=run_id,
                    )
            except asyncio.CancelledError:
                # Shutdown cut this short. Leave no epilogue: the lease lapses
                # and a supervisor resumes from the last checkpoint.
                bound.warning("run cancelled mid-flight; left for recovery")
                raise
            except BaseException as exc:
                app.state.errors[run_id] = f"{type(exc).__name__}: {exc}"
                bound.error("run failed", error=str(exc))
            finally:
                app.state.inflight.pop(run_id, None)

        app.state.inflight[run_id] = asyncio.create_task(execute(), name=f"run-{run_id}")
        return RunAccepted(run_id=run_id, status="accepted", href=f"/runs/{run_id}")

    @app.get("/runs")
    async def list_runs(
        who: Caller, limit: int = Query(default=50, ge=1, le=500)
    ) -> list[dict[str, Any]]:
        del who
        rows: list[dict[str, Any]] = await app.state.forge.runs(limit=limit)
        return rows

    @app.get("/runs/{run_id}", response_model=RunView)
    async def get_run(run_id: str, who: Caller) -> RunView:
        """Current state, projected from the event log."""
        del who
        events = await app.state.forge.events(run_id)
        if not events:
            if run_id in app.state.inflight:
                return RunView(run_id=run_id, status="PENDING")
            raise HTTPException(status_code=404, detail=f"no such run: {run_id}")

        state = project(events)
        owner = None
        if app.state.leases is not None:
            lease = await app.state.leases.get(run_id)
            owner = lease.owner if lease and lease.is_live() else None

        return RunView(
            run_id=run_id,
            status=state.status.value,
            answer=state.answer,
            steps=state.steps_committed,
            tokens=state.usage.total_tokens,
            usd=state.usage.usd,
            resumed=state.resumes > 0,
            owned_by=owner,
            error=app.state.errors.get(run_id),
        )

    @app.get("/runs/{run_id}/events")
    async def get_events(
        run_id: str, who: Caller, after_seq: int = Query(default=0, ge=0)
    ) -> list[dict[str, Any]]:
        """The durable audit trail. `after_seq` makes this pollable."""
        del who
        events = await app.state.forge.events(run_id, after_seq=after_seq)
        if not events and after_seq == 0 and run_id not in app.state.inflight:
            raise HTTPException(status_code=404, detail=f"no such run: {run_id}")
        return [
            {
                "seq": e.seq,
                "ts": e.ts.isoformat(),
                "type": e.type.value,
                "step_index": e.step_index,
                "payload": e.payload,
            }
            for e in events
        ]

    @app.post("/runs/{run_id}/resume", status_code=202, response_model=RunAccepted)
    async def resume_run(run_id: str, who: Caller) -> RunAccepted:
        """Continue an interrupted run. Idempotent: a finished run is a no-op."""
        _reject_if_draining()
        events = await app.state.forge.events(run_id)
        if not events:
            raise HTTPException(status_code=404, detail=f"no such run: {run_id}")

        state = project(events)
        if state.status in (RunStatus.COMPLETED, RunStatus.ABORTED):
            return RunAccepted(run_id=run_id, status=state.status.value, href=f"/runs/{run_id}")

        supervisor: Supervisor | None = app.state.supervisor
        if supervisor is not None:
            lease = await app.state.leases.get(run_id)
            if lease is not None and lease.is_live() and lease.owner != supervisor.owner:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"run is already owned by {lease.owner}",
                )

        async def execute() -> None:
            try:
                if supervisor is not None:
                    async with supervisor.owning(run_id):
                        await app.state.forge.resume(run_id)
                else:
                    await app.state.forge.resume(run_id)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                app.state.errors[run_id] = f"{type(exc).__name__}: {exc}"
                log.error("resume failed", run_id=run_id, error=str(exc))
            finally:
                app.state.inflight.pop(run_id, None)

        app.state.inflight[run_id] = asyncio.create_task(execute(), name=f"resume-{run_id}")
        log.info("run resuming", run_id=run_id, principal=who.label)
        return RunAccepted(run_id=run_id, status="resuming", href=f"/runs/{run_id}")

    @app.get("/runs/{run_id}/checkpoint")
    async def get_checkpoint(run_id: str, who: Caller) -> dict[str, Any]:
        del who
        ckpt = await app.state.forge.checkpoint(run_id)
        if ckpt is None:
            raise HTTPException(status_code=404, detail="no checkpoint for this run")
        return {
            "id": ckpt.id,
            "step_index": ckpt.step_index,
            "last_seq": ckpt.last_seq,
            "kind": ckpt.kind,
            "created_at": ckpt.created_at.isoformat() if ckpt.created_at else None,
        }

    # -- console -----------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def console() -> HTMLResponse:
        """The operator console.

        Unauthenticated on purpose: the page is markup and contains no run
        data. Every request it subsequently makes carries the operator's API
        key, so the authorization boundary is unchanged - serving this does
        not widen what an anonymous caller can read.
        """
        return HTMLResponse(DASHBOARD_HTML)

    # -- operations --------------------------------------------------------

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        """Liveness: the process is up. Deliberately touches nothing else, so
        a slow dependency cannot get the container killed."""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, Any]:
        """Readiness: can this replica take traffic right now?"""
        if app.state.draining:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"ok": False, "reason": "draining"}
        if auth.misconfigured:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"ok": False, "reason": "auth required but no API keys configured"}
        health = await app.state.forge.health()
        response.status_code = 200 if health["ok"] else status.HTTP_503_SERVICE_UNAVAILABLE
        return {"ok": bool(health["ok"]), **health}

    @app.get("/healthz")
    async def healthz(response: Response) -> dict[str, Any]:
        health: dict[str, Any] = await app.state.forge.health()
        health["inflight"] = len(app.state.inflight)
        if app.state.supervisor is not None:
            health["supervisor"] = app.state.supervisor.stats()
        response.status_code = 200 if health["ok"] else status.HTTP_503_SERVICE_UNAVAILABLE
        return health

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(
            content=app.state.forge.metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/policy")
    async def policy(who: Caller) -> dict[str, Any]:
        """What this deployment will and will not authorize."""
        del who
        bundle = app.state.forge.policy
        return {
            "version": bundle.version,
            "budget": {
                "max_usd": bundle.budget.max_usd,
                "max_tokens": bundle.budget.max_tokens,
                "max_steps": bundle.budget.max_steps,
                "max_tool_calls": bundle.budget.max_tool_calls,
            },
            "capabilities": {
                name: {
                    "granted": grant.granted,
                    "requires_approval": grant.requires_approval,
                    "allowed_effects": sorted(e.value for e in grant.allowed_effects),
                    "max_invocations": grant.max_invocations,
                }
                for name, grant in sorted(bundle.capabilities.items())
            },
        }

    @app.get("/config")
    async def get_config(who: Caller) -> dict[str, Any]:
        """Redacted effective configuration. Never includes secrets."""
        del who
        described: dict[str, Any] = app.state.config.describe()
        return described

    return app


async def _drain(app: FastAPI, timeout_s: float = 25.0) -> None:
    """Wait for in-flight runs, then cancel whatever is left.

    Anything cancelled is *not* lost: it holds a lease that will lapse, and a
    supervisor resumes it from the last checkpoint. Draining is an
    optimisation for the common case, not the safety mechanism.
    """
    tasks = list(app.state.inflight.values())
    if not tasks:
        return
    log.info("draining", inflight=len(tasks), timeout_s=timeout_s)
    done, pending = await asyncio.wait(tasks, timeout=timeout_s)
    for task in pending:
        task.cancel()
    if pending:
        log.warning("left runs for recovery", finished=len(done), cancelled=len(pending))
        await asyncio.gather(*pending, return_exceptions=True)


def main() -> None:  # pragma: no cover - console entry point
    import uvicorn

    uvicorn.run("forge.api:create_app", factory=True, host="0.0.0.0", port=8080)
