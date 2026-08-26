"""The HTTP adapter against a real server.

Every other test reaches the app through `TestClient`, which calls the ASGI
app in-process. That verifies the routes but not the *transport*: real
sockets, real status codes, real polling, and a `HttpTarget` that has to
discover a run has finished by asking.

This is the test that proves the same case set runs against a deployed
service, which is the claim the adapter layer exists to make.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

import uvicorn

from forge.config import BudgetConfig, ForgeConfig, ProviderConfig
from forge.deployment import Forge
from forge.eval import CaseSet, Harness, HarnessConfig, HttpTarget
from forge.llm.mock import MockProvider
from tests.conftest import run

pytestmark = pytest.mark.slow

SCRIPT = [
    {"proposal": {"kind": "TOOL_CALL", "tool": "search_corpus",
                  "arguments": {"query": "context"}}},
    {"proposal": {"kind": "TOOL_CALL", "tool": "read_document",
                  "arguments": {"key": "context"}}},
    {"proposal": {"kind": "ANSWER",
                  "answer": "Context compilation reduced token usage by 38%."}},
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def live_server():
    """A real uvicorn server on a real port, torn down afterwards."""
    from forge.api.app import create_app

    tmp = Path(tempfile.mkdtemp())
    config = ForgeConfig(
        database_url=f"sqlite:///{tmp / 'e2e.db'}",
        providers=(ProviderConfig(kind="mock"),),
        budget=BudgetConfig(max_steps=8),
        tools=("search_corpus", "read_document"),
    )
    deployment = Forge(config=config, providers=[MockProvider(SCRIPT)])
    app = create_app(config, deployment=deployment, require_auth=False, configure_logs=False)

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.skip("uvicorn did not start in time")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def test_case_set_runs_against_a_live_service(live_server) -> None:
    """The adapter-layer claim, exercised over real HTTP.

    Note the trajectory graders: passing them requires the service to have
    served a real event log through `/runs/{id}/events`, not just an answer.
    """
    cases = CaseSet.load("cases").select(ids=["retrieval.context-compilation"])
    target = HttpTarget(live_server, poll_interval_s=0.2)

    results = run(Harness(cases, target, config=HarnessConfig(concurrency=1)).run())
    record = results.records[0]

    assert results.green, record.error
    assert record.target_version.startswith("http/")
    assert {g["kind"] for g in record.grades} >= {"tool_used", "no_duplicate_effects"}
    assert all(g["passed"] for g in record.grades)


def test_unreachable_service_is_reported_as_infrastructure() -> None:
    """A dead service is not a verdict on the target's behaviour."""
    from forge.eval import Outcome

    cases = CaseSet.load("cases").select(ids=["retrieval.context-compilation"])
    target = HttpTarget(f"http://127.0.0.1:{_free_port()}")

    results = run(
        Harness(
            cases, target,
            config=HarnessConfig(max_infra_retries=0, retry_backoff_s=0.0),
        ).run()
    )
    assert results.records[0].outcome == Outcome.TARGET_UNAVAILABLE.value
    assert results.verdicts == [], "an unreachable service must not affect the pass rate"


def test_live_service_enforces_run_ownership(live_server) -> None:
    """Two clients cannot both drive the same run."""
    import httpx

    async def main():
        async with httpx.AsyncClient(base_url=live_server, timeout=30.0) as client:
            started = await client.post(
                "/runs", json={"goal": "What did FORGE measure about context compilation?"}
            )
            run_id = started.json()["run_id"]

            for _ in range(200):
                view = await client.get(f"/runs/{run_id}")
                if view.status_code == 200 and view.json()["status"] == "COMPLETED":
                    break
                await asyncio.sleep(0.05)

            again = await client.post(f"/runs/{run_id}/resume")
            return view.json(), again.json()

    finished, resumed = run(main())
    assert finished["status"] == "COMPLETED"
    assert resumed["status"] == "COMPLETED", "resuming a finished run must be a no-op"
