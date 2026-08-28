"""The operator console.

This runtime's argument is that every effect is authorized, recorded and
recoverable. Until now the only way to see that was `forge trace` or raw JSON,
which means the thing the product is *for* was the thing hardest to look at.

These tests cover the two properties that matter for a UI bolted onto an
authenticated service: it must be genuinely self-contained (no CDN, no build
step, no fifth dependency), and serving it must not widen what an anonymous
caller can read.
"""

from __future__ import annotations

import re
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from forge.api.dashboard import DASHBOARD_HTML
from forge.config import BudgetConfig, ForgeConfig, ProviderConfig
from forge.deployment import Forge
from forge.llm.mock import MockProvider

SCRIPT = [
    {"proposal": {"kind": "TOOL_CALL", "tool": "calculate",
                  "arguments": {"expression": "38 * 2"}}},
    {"proposal": {"kind": "ANSWER", "answer": "76"}},
]

KEYS = {  # sha256("secret-key") -> label
    "d1a2b0f8a7fbbcb2b3cbeb0ae44e0d6a41ff8b2e0e9d0f4b1f0e7d0a9c1b2d3e": "test"
}


def _config(tmp_path) -> ForgeConfig:
    return ForgeConfig(
        database_url=f"sqlite:///{tmp_path / 'console.db'}",
        providers=(ProviderConfig(kind="mock"),),
        budget=BudgetConfig(max_steps=6),
        tools=("calculate",),
    )


def _client(tmp_path, **kwargs) -> TestClient:
    from forge.api.app import create_app

    deployment = Forge(config=_config(tmp_path), providers=[MockProvider(SCRIPT)])
    app = create_app(
        _config(tmp_path), deployment=deployment, configure_logs=False,
        **{"require_auth": False, **kwargs},
    )
    return TestClient(app)


# -- it is served, and it is a page -----------------------------------------


def test_the_console_is_served_at_the_root(tmp_path) -> None:
    with _client(tmp_path) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "FORGE console" in resp.text


def test_the_console_needs_no_credential_but_the_api_still_does(tmp_path) -> None:
    """Serving markup must not widen the authorization boundary.

    The page contains no run data; every request it makes carries the
    operator's key. So `/` is open and `/runs` is not, and that difference is
    the whole security argument for shipping it this way.
    """
    with _client(tmp_path, require_auth=True, api_keys=KEYS) as client:
        assert client.get("/").status_code == 200
        assert client.get("/runs").status_code == 401


# -- self-contained ----------------------------------------------------------


def test_the_console_loads_nothing_from_the_network() -> None:
    """No CDN, no font host, no analytics. It has to work on an air-gapped box,
    and a console that phones home would undercut the audit story it exists to
    tell."""
    external = re.findall(r'(?:src|href)\s*=\s*["\'](https?:)?//[^"\']+', DASHBOARD_HTML)
    assert external == [], f"console references external resources: {external}"
    assert "cdn" not in DASHBOARD_HTML.lower()


def test_the_console_only_calls_endpoints_that_exist(tmp_path) -> None:
    """A UI referring to a route that does not exist is a broken UI that no
    test would otherwise catch."""
    called = set(re.findall(r'api\("(/[^"]*)"', DASHBOARD_HTML))
    called |= {m for m in re.findall(r'api\("(/runs)"', DASHBOARD_HTML)}
    literal = {c for c in called if "+" not in c and "$" not in c}
    assert "/runs" in literal

    with _client(tmp_path) as client:
        for path in literal:
            assert client.get(path).status_code != 404, f"console calls missing route {path}"


def test_the_console_sends_the_key_as_a_bearer_token() -> None:
    """It must match the scheme `_extract` on the server actually reads.

    A console that sent `X-Api-Token` would authenticate against nothing and
    fail with a 401 that looks like the operator's fault.
    """
    import inspect

    from forge.api import security

    assert "Authorization" in DASHBOARD_HTML
    assert '"Bearer " + key' in DASHBOARD_HTML

    # The server accepts `Authorization: Bearer <key>` or `X-API-Key`. The
    # console uses the former, so that branch has to exist server-side.
    assert "bearer " in inspect.getsource(security._extract_token)


# -- the listing it renders --------------------------------------------------


def test_the_run_listing_carries_a_status(tmp_path) -> None:
    """Without this the console shows every run as PENDING.

    Derived in SQL from the terminal event rather than by projecting each run:
    a fifty-run listing should not be fifty folds.
    """
    with _client(tmp_path) as client:
        run_id = client.post(
            "/runs", json={"goal": "Compute 38 * 2", "tools": ["calculate"]}
        ).json()["run_id"]

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            body = client.get(f"/runs/{run_id}").json()
            if body["status"] in ("COMPLETED", "FAILED", "ABORTED"):
                break
            time.sleep(0.05)

        rows = client.get("/runs").json()
        row = next(r for r in rows if r["run_id"] == run_id)
        assert row["status"] == "COMPLETED"
        assert row["events"] > 0


def test_an_unfinished_run_lists_as_running(tmp_path) -> None:
    """No terminal event means it has not terminated. The run view, which does
    project, is what tells RUNNING from INTERRUPTED."""
    from forge.core.enums import EventType
    from forge.core.events import NewEvent
    from forge.state.sqlite_store import SQLiteEventStore
    from tests.conftest import run as drive

    store = SQLiteEventStore(tmp_path / "listing.db")

    async def scenario() -> list[dict]:
        await store.open()
        await store.append(
            NewEvent(type=EventType.RUN_CREATED, run_id="run_open", payload={"goal": "g"})
        )
        await store.append(
            NewEvent(type=EventType.RUN_CREATED, run_id="run_shut", payload={"goal": "g"})
        )
        await store.append(
            NewEvent(type=EventType.RUN_FAILED, run_id="run_shut", payload={})
        )
        rows = await store.list_runs()
        await store.close()
        return rows

    rows = {r["run_id"]: r["status"] for r in drive(scenario())}
    assert rows["run_open"] == "RUNNING"
    assert rows["run_shut"] == "FAILED"


# -- the workbench ---------------------------------------------------------


def test_the_workbench_is_absent_without_a_repository(tmp_path) -> None:
    """A deployment serving generic runs gets no endpoints that read or write
    a working tree. That is the right default, not a setting to remember."""
    with _client(tmp_path) as client:
        assert client.get("/code").status_code == 404
        assert client.get("/code/tree").status_code == 404


def test_the_workbench_is_served_when_a_repository_is_mounted(tmp_path) -> None:
    import subprocess

    from forge.api.app import create_app
    from forge.deployment import Forge
    from forge.llm.mock import MockProvider

    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    for args in (
        ["init", "-q", "-b", "master"], ["config", "user.email", "t@t.t"],
        ["config", "user.name", "t"], ["add", "-A"], ["commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    deployment = Forge(config=_config(tmp_path), providers=[MockProvider(SCRIPT)])
    app = create_app(
        _config(tmp_path), deployment=deployment, configure_logs=False,
        require_auth=False, repo=str(root),
    )
    with TestClient(app) as client:
        page = client.get("/code")
        assert page.status_code == 200
        assert "FORGE workbench" in page.text

        tree = client.get("/code/tree").json()
        assert "a.py" in tree["files"]
        assert client.get("/code/status").json()["branch"] == "master"


def test_the_workbench_loads_nothing_from_the_network() -> None:
    from forge.api.workbench import WORKBENCH_HTML

    external = re.findall(r'(?:src|href)\s*=\s*["\'](https?:)?//[^"\']+', WORKBENCH_HTML)
    assert external == [], f"workbench references external resources: {external}"
