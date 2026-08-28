"""First-run experience: diagnostics that name the fix, and a service that
does not quietly start without a lock on the door.

Both of these were found by breaking things a new user would break, rather
than by reading the code. That is the only way this class of defect surfaces:
every one of them is invisible when the machine is already set up correctly.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from forge.llm.ollama import OllamaProvider
from tests.conftest import run


def _provider(handler: Any, *, model: str = "qwen3:8b") -> OllamaProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://ollama.test")
    return OllamaProvider(model=model, client=client)


def _tags(*names: str) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": n} for n in names]})
        return httpx.Response(404)

    return handler


# -- the diagnosis --------------------------------------------------------


def test_a_pulled_model_is_healthy() -> None:
    provider = _provider(_tags("qwen3:8b", "llama3.2:1b"))
    assert run(provider.diagnose()) is None
    assert run(provider.healthy()) is True


def test_a_missing_model_names_the_pull_command() -> None:
    """The defect this exists to stop.

    `doctor` used to check only that the daemon answered, so a model that was
    never pulled reported "ready" and the very next command died with a bare
    `ollama returned 404`. A diagnostic contradicted by the next command is
    worse than none: it sends the user looking in the wrong place.
    """
    provider = _provider(_tags("llama3.2:1b"), model="mistral-nemo:12b")

    detail = run(provider.diagnose())
    assert detail is not None
    assert "not pulled" in detail
    assert "ollama pull mistral-nemo:12b" in detail
    assert run(provider.healthy()) is False, "this provider cannot serve a request"


def test_an_unreachable_daemon_names_the_serve_command() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    detail = run(_provider(refuse).diagnose())
    assert detail is not None
    assert "ollama serve" in detail
    assert "is it running" in detail


def test_a_bare_name_matches_the_latest_tag() -> None:
    """Ollama treats `qwen3` as `qwen3:latest`. Telling someone to pull what
    they already have is its own kind of wrong answer."""
    provider = _provider(_tags("qwen3:latest"), model="qwen3")
    assert run(provider.diagnose()) is None


def test_a_tagged_name_is_not_satisfied_by_a_different_tag() -> None:
    provider = _provider(_tags("qwen3:1.7b"), model="qwen3:8b")
    detail = run(provider.diagnose())
    assert detail is not None and "not pulled" in detail


def test_an_error_status_from_the_daemon_is_reported_as_such() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    detail = run(_provider(broken).diagnose())
    assert detail is not None and "500" in detail


# -- the service announcement --------------------------------------------


def test_serving_without_a_key_on_loopback_is_allowed_but_loud(
    monkeypatch, capsys
) -> None:
    """Keyless on a laptop is genuinely useful, so it stays permitted. It just
    stops being silent: this endpoint executes tools and spends money."""
    from forge.cli import _announce

    monkeypatch.delenv("FORGE_API_KEYS", raising=False)
    _announce("127.0.0.1", 8080)

    out = capsys.readouterr().out
    assert "http://127.0.0.1:8080" in out
    assert "auth      DISABLED" in out
    assert "FORGE_API_KEYS" in out


def test_serving_without_a_key_on_a_public_interface_is_refused(monkeypatch) -> None:
    """Loopback keyless is a convenience; 0.0.0.0 keyless is an open agent
    runtime on the network, and no warning makes that acceptable."""
    import typer

    from forge.cli import _announce

    monkeypatch.delenv("FORGE_API_KEYS", raising=False)
    with pytest.raises(typer.Exit) as caught:
        _announce("0.0.0.0", 8080)
    assert caught.value.exit_code == 2


def test_a_key_lets_a_public_bind_through(monkeypatch, capsys) -> None:
    from forge.cli import _announce

    monkeypatch.setenv("FORGE_API_KEYS", "ops:s3cret")
    _announce("0.0.0.0", 8080)

    out = capsys.readouterr().out
    assert "auth      enabled" in out
    assert "s3cret" not in out, "the key must never be echoed"


def test_the_console_url_is_printed_for_a_public_bind(monkeypatch, capsys) -> None:
    """0.0.0.0 is not a URL anyone can open; show something clickable."""
    from forge.cli import _announce

    monkeypatch.setenv("FORGE_API_KEYS", "ops:s3cret")
    _announce("0.0.0.0", 9000)
    assert "http://127.0.0.1:9000" in capsys.readouterr().out


def test_ui_is_a_registered_command() -> None:
    """The one-command entry point for someone who just wants to look at it."""
    from forge.cli import app

    names = {c.name or c.callback.__name__ for c in app.registered_commands}
    assert "ui" in names
