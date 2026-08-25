"""FastAPI service around a `Harness`.

    uvicorn "forge.api:create_app" --factory --port 8080

Design notes worth stating, because they are what separate a service from a
script with routes bolted on:

* **Runs are asynchronous.** `POST /runs` returns `202` with a run id
  immediately. A long-horizon agent run can take minutes; holding an HTTP
  connection open for it is how you turn a client timeout into an orphaned
  side effect.
* **The event log is the read model.** `GET /runs/{id}/events` streams from
  the same durable log the runtime writes, so an observer sees exactly what
  happened, not a summary someone remembered to update.
* **Interruption is expected, not exceptional.** `POST /runs/{id}/resume`
  is a first-class endpoint; recovering a run is normal operation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field

from forge import __version__
from forge.config import ForgeConfig
from forge.core.enums import RunStatus
from forge.harness import Harness
from forge.ids import new_id
from forge.state.projection import project

__all__ = ["create_app"]


class RunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    tools: list[str] | None = None
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
    error: str | None = None


def create_app(config: ForgeConfig | None = None, *, harness: Harness | None = None) -> FastAPI:
    """Build the ASGI app. Accepts an injected harness for testing."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await app.state.harness.open()
        try:
            yield
        finally:
            await app.state.harness.close()

    app = FastAPI(
        title="FORGE",
        version=__version__,
        summary="Durable, policy-aware execution runtime for long-horizon AI agents.",
        lifespan=lifespan,
    )
    app.state.harness = harness or Harness(config=config or ForgeConfig.load())
    app.state.inflight = {}

    # -- runs --------------------------------------------------------------

    @app.post("/runs", status_code=202, response_model=RunAccepted)
    async def start_run(body: RunRequest, background: BackgroundTasks) -> RunAccepted:
        """Accept a task and execute it in the background."""
        h: Harness = app.state.harness
        run_id = new_id("run")

        async def execute() -> None:
            try:
                await h.run(
                    body.goal, tools=body.tools, max_steps=body.max_steps, run_id=run_id
                )
            except BaseException as exc:  # noqa: BLE001 - recorded, never swallowed silently
                app.state.inflight[run_id] = f"{type(exc).__name__}: {exc}"
            else:
                app.state.inflight.pop(run_id, None)

        background.add_task(execute)
        return RunAccepted(run_id=run_id, status="accepted", href=f"/runs/{run_id}")

    @app.get("/runs", response_model=list[dict])
    async def list_runs(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, Any]]:
        return await app.state.harness.runs(limit=limit)

    @app.get("/runs/{run_id}", response_model=RunView)
    async def get_run(run_id: str) -> RunView:
        """Current state, projected from the event log."""
        events = await app.state.harness.events(run_id)
        if not events:
            raise HTTPException(status_code=404, detail=f"no such run: {run_id}")
        state = project(events)
        return RunView(
            run_id=run_id,
            status=state.status.value,
            answer=state.answer,
            steps=state.steps_committed,
            tokens=state.usage.total_tokens,
            usd=state.usage.usd,
            resumed=state.resumes > 0,
            error=app.state.inflight.get(run_id),
        )

    @app.get("/runs/{run_id}/events")
    async def get_events(
        run_id: str, after_seq: int = Query(default=0, ge=0)
    ) -> list[dict[str, Any]]:
        """The durable audit trail. `after_seq` makes this pollable."""
        events = await app.state.harness.events(run_id, after_seq=after_seq)
        if not events and after_seq == 0:
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
    async def resume_run(run_id: str, background: BackgroundTasks) -> RunAccepted:
        """Continue an interrupted run. Idempotent: a finished run is a no-op."""
        events = await app.state.harness.events(run_id)
        if not events:
            raise HTTPException(status_code=404, detail=f"no such run: {run_id}")
        state = project(events)
        if state.status in (RunStatus.COMPLETED, RunStatus.ABORTED):
            return RunAccepted(run_id=run_id, status=state.status.value, href=f"/runs/{run_id}")

        async def execute() -> None:
            try:
                await app.state.harness.resume(run_id)
            except BaseException as exc:  # noqa: BLE001
                app.state.inflight[run_id] = f"{type(exc).__name__}: {exc}"

        background.add_task(execute)
        return RunAccepted(run_id=run_id, status="resuming", href=f"/runs/{run_id}")

    @app.get("/runs/{run_id}/checkpoint")
    async def get_checkpoint(run_id: str) -> dict[str, Any]:
        ckpt = await app.state.harness.checkpoint(run_id)
        if ckpt is None:
            raise HTTPException(status_code=404, detail="no checkpoint for this run")
        return {
            "id": ckpt.id,
            "step_index": ckpt.step_index,
            "last_seq": ckpt.last_seq,
            "kind": ckpt.kind,
            "created_at": ckpt.created_at.isoformat() if ckpt.created_at else None,
        }

    # -- operations --------------------------------------------------------

    @app.get("/healthz")
    async def healthz(response: Response) -> dict[str, Any]:
        health = await app.state.harness.health()
        response.status_code = 200 if health["ok"] else 503
        return health

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        """Liveness: the process is up. Deliberately touches nothing else."""
        return {"status": "ok", "version": __version__}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(
            content=app.state.harness.metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/policy")
    async def policy() -> dict[str, Any]:
        """What this deployment will and will not authorize."""
        bundle = app.state.harness.policy
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
    async def get_config() -> dict[str, Any]:
        """Redacted effective configuration. Never includes secrets."""
        return app.state.harness.config.describe()

    return app


def main() -> None:  # pragma: no cover - console entry point
    import uvicorn

    uvicorn.run("forge.api:create_app", factory=True, host="0.0.0.0", port=8080)
