"""What makes Studio installable software rather than a page you visit.

Chromium will only offer "Install" when three things hold at once: a manifest
with 192px and 512px icons and a standalone display mode, a service worker with
a fetch handler, and a secure origin. Studio serves on loopback, which counts
as secure, so the other two are testable here - and each is the sort of thing
that breaks silently, because the only symptom is an install button that never
appears.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from forge.api.pwa import MANIFEST, SERVICE_WORKER
from forge.config import BudgetConfig, ForgeConfig, ProviderConfig
from forge.deployment import Forge
from forge.llm.mock import MockProvider


def _client(tmp_path: Path) -> TestClient:
    from forge.api.app import create_app

    config = ForgeConfig(
        database_url=f"sqlite:///{tmp_path / 'pwa.db'}",
        providers=(ProviderConfig(kind="mock"),),
        budget=BudgetConfig(max_steps=4),
        tools=("calculate",),
    )
    deployment = Forge(config=config, providers=[MockProvider([])])
    return TestClient(
        create_app(config, deployment=deployment, configure_logs=False, require_auth=False)
    )


# -- the manifest ----------------------------------------------------------


def test_the_manifest_meets_the_install_criteria(tmp_path) -> None:
    with _client(tmp_path) as client:
        resp = client.get("/manifest.webmanifest")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/manifest+json")

        m = json.loads(resp.text)
        sizes = {i["sizes"] for i in m["icons"]}
        assert m["display"] == "standalone", "a browser tab is not an application"
        assert "192x192" in sizes and "512x512" in sizes, "Chromium requires both"
        assert m["start_url"] == "/code", "installing should open the editor, not the console"
        assert any(i.get("purpose") == "maskable" for i in m["icons"])


def test_the_manifest_is_reachable_without_a_credential(tmp_path) -> None:
    """It carries no repository data, and an unreadable manifest is an app
    that can never be installed."""
    KEYS = {"d1a2b0f8a7fbbcb2b3cbeb0ae44e0d6a41ff8b2e0e9d0f4b1f0e7d0a9c1b2d3e": "t"}
    from forge.api.app import create_app

    config = ForgeConfig(
        database_url=f"sqlite:///{tmp_path / 'pwa2.db'}",
        providers=(ProviderConfig(kind="mock"),),
    )
    app = create_app(config, deployment=Forge(config=config, providers=[MockProvider([])]),
                     configure_logs=False, require_auth=True, api_keys=KEYS)
    with TestClient(app) as client:
        assert client.get("/manifest.webmanifest").status_code == 200
        assert client.get("/icon.svg").status_code == 200
        assert client.get("/sw.js").status_code == 200
        assert client.get("/runs").status_code == 401, "data is still guarded"


def test_the_icon_is_a_real_svg(tmp_path) -> None:
    with _client(tmp_path) as client:
        resp = client.get("/icon.svg")
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert resp.text.lstrip().startswith("<svg")
        assert "viewBox" in resp.text


# -- the service worker ----------------------------------------------------


def test_the_service_worker_has_a_fetch_handler(tmp_path) -> None:
    """Without one, Chromium will not offer to install, no matter how good
    the manifest is."""
    with _client(tmp_path) as client:
        body = client.get("/sw.js").text
        assert 'addEventListener("fetch"' in body
        assert 'addEventListener("install"' in body


def test_the_service_worker_is_never_cached(tmp_path) -> None:
    """A cached service worker is how an app gets stuck on a stale shell for
    as long as the browser feels like it."""
    with _client(tmp_path) as client:
        assert client.get("/sw.js").headers["cache-control"] == "no-store"


def test_live_data_is_never_served_from_cache() -> None:
    """The repository and the agent are live. A cached branch is a lie told
    confidently, so only the shell may come from cache."""
    assert 'url.pathname.startsWith("/code/")' in SERVICE_WORKER
    assert 'url.pathname.startsWith("/runs")' in SERVICE_WORKER
    # The data branch must return before the caching branch is reached.
    data_at = SERVICE_WORKER.index("const isData")
    cache_at = SERVICE_WORKER.index("caches.match(event.request)")
    assert data_at < cache_at, "the cache path must not be able to catch live data"


def test_the_shell_cache_lists_only_shell_files() -> None:
    shell = SERVICE_WORKER[SERVICE_WORKER.index("SHELL_FILES"):]
    listed = shell[: shell.index("]")]
    assert "/code/" not in listed and "/runs" not in listed


# -- desktop integration ---------------------------------------------------


def test_the_shortcut_goes_where_this_platform_expects() -> None:
    from forge.desktop import shortcut_location

    where = shortcut_location()
    if sys.platform == "win32":
        assert where.suffix == ".lnk"
        assert "Start Menu" in str(where)
    elif sys.platform == "darwin":
        assert where.suffix == ".command"
    else:
        assert where.suffix == ".desktop"


def test_the_launcher_uses_this_interpreter_not_a_bare_command() -> None:
    """A shortcut made inside a virtual environment has to keep working after
    the shell that made it is gone, and PATH is not something a desktop
    launcher inherits."""
    from forge.desktop import _launcher_command

    command = _launcher_command(Path.cwd(), 8080)
    assert command[0] == sys.executable
    assert command[1:3] == ["-m", "forge.cli"]
    assert "studio" in command


def test_installing_reports_failure_rather_than_raising(tmp_path, monkeypatch) -> None:
    """A shortcut is a convenience. Failing to create one must not take the
    command down with it."""
    from forge import desktop

    monkeypatch.setattr(
        desktop, "shortcut_location",
        lambda name="FORGE Studio": tmp_path / "nope" / "\0bad" / "x.lnk",
    )
    entry = desktop.install_shortcut(tmp_path)
    assert entry.created is False
    assert entry.note, "a failure must say what went wrong"


def test_manifest_and_worker_are_plain_data() -> None:
    """Both are served verbatim; a syntax error would only show up as an app
    that silently refuses to install."""
    assert json.loads(json.dumps(MANIFEST))["name"] == "FORGE Studio"
    assert SERVICE_WORKER.count("{") == SERVICE_WORKER.count("}")
    assert SERVICE_WORKER.count("(") == SERVICE_WORKER.count(")")
