"""HTTP service behaviour.

Covers the surface an operator and a client actually touch: authentication,
rate limiting, the asynchronous run lifecycle, readiness semantics, and the
ownership rules that make recovery safe across replicas.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

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
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        providers=(ProviderConfig(kind="mock"),),
        budget=BudgetConfig(max_steps=6),
        tools=("calculate",),
    )


def _client(tmp_path, **kwargs) -> TestClient:
    from forge.api.app import create_app

    deployment = Forge(config=_config(tmp_path), providers=[MockProvider(SCRIPT)])
    app = create_app(
        _config(tmp_path),
        deployment=deployment,
        configure_logs=False,
        **{"require_auth": False, **kwargs},
    )
    return TestClient(app)


def _await_run(client: TestClient, run_id: str, *, timeout_s: float = 15.0) -> dict:
    """Poll until the run reaches a terminal state.

    Polling is the honest client pattern here: `POST /runs` is deliberately
    asynchronous, so the test exercises the same flow a real caller uses.
    """
    deadline = time.monotonic() + timeout_s
    body: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/runs/{run_id}")
        if resp.status_code == 200:
            body = resp.json()
            if body["status"] in ("COMPLETED", "FAILED", "ABORTED"):
                return body
        time.sleep(0.05)
    return body


# ── the run lifecycle ───────────────────────────────────────────────────


def test_post_runs_accepts_immediately_and_completes(tmp_path) -> None:
    """202 now, answer later. A long run must not hold the connection."""
    with _client(tmp_path) as client:
        resp = client.post("/runs", json={"goal": "Compute 38 * 2", "tools": ["calculate"]})
        assert resp.status_code == 202
        run_id = resp.json()["run_id"]
        assert resp.json()["href"] == f"/runs/{run_id}"

        body = _await_run(client, run_id)
        assert body["status"] == "COMPLETED"
        assert body["answer"] == "76"
        assert body["steps"] >= 1


def test_events_endpoint_serves_the_durable_log(tmp_path) -> None:
    with _client(tmp_path) as client:
        run_id = client.post("/runs", json={"goal": "Compute 38 * 2"}).json()["run_id"]
        _await_run(client, run_id)

        events = client.get(f"/runs/{run_id}/events").json()
        kinds = {e["type"] for e in events}
        assert {"RUN_CREATED", "POLICY_DECIDED", "EFFECT_OBSERVED", "RUN_COMPLETED"} <= kinds
        assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)


def test_events_after_seq_supports_polling(tmp_path) -> None:
    with _client(tmp_path) as client:
        run_id = client.post("/runs", json={"goal": "Compute 38 * 2"}).json()["run_id"]
        _await_run(client, run_id)

        everything = client.get(f"/runs/{run_id}/events").json()
        midpoint = everything[len(everything) // 2]["seq"]
        tail = client.get(f"/runs/{run_id}/events", params={"after_seq": midpoint}).json()
        assert tail and all(e["seq"] > midpoint for e in tail)


def test_unknown_run_is_404(tmp_path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/runs/run_nope").status_code == 404
        assert client.get("/runs/run_nope/events").status_code == 404
        assert client.post("/runs/run_nope/resume").status_code == 404


def test_resuming_a_finished_run_is_a_no_op(tmp_path) -> None:
    """Idempotent by design: a client retrying resume must not redo work."""
    with _client(tmp_path) as client:
        run_id = client.post("/runs", json={"goal": "Compute 38 * 2"}).json()["run_id"]
        finished = _await_run(client, run_id)

        resp = client.post(f"/runs/{run_id}/resume")
        assert resp.status_code == 202
        assert resp.json()["status"] == "COMPLETED"
        assert client.get(f"/runs/{run_id}").json()["steps"] == finished["steps"]


def test_checkpoint_endpoint_reports_the_watermark(tmp_path) -> None:
    with _client(tmp_path) as client:
        run_id = client.post("/runs", json={"goal": "Compute 38 * 2"}).json()["run_id"]
        _await_run(client, run_id)
        ckpt = client.get(f"/runs/{run_id}/checkpoint").json()
        assert ckpt["step_index"] >= 1
        assert ckpt["last_seq"] > 0


# ── request validation ──────────────────────────────────────────────────


def test_oversized_and_empty_goals_are_rejected(tmp_path) -> None:
    with _client(tmp_path) as client:
        assert client.post("/runs", json={"goal": ""}).status_code == 422
        assert client.post("/runs", json={"goal": "x" * 9000}).status_code == 422
        assert client.post("/runs", json={"goal": "ok", "max_steps": 9999}).status_code == 422


# ── authentication ──────────────────────────────────────────────────────


def test_auth_rejects_missing_and_wrong_keys(tmp_path) -> None:
    import hashlib

    keys = {hashlib.sha256(b"right-key").hexdigest(): "ci"}
    with _client(tmp_path, require_auth=True, api_keys=keys) as client:
        assert client.post("/runs", json={"goal": "x"}).status_code == 401
        assert client.post(
            "/runs", json={"goal": "x"}, headers={"Authorization": "Bearer wrong"}
        ).status_code == 401

        ok = client.post(
            "/runs", json={"goal": "Compute 38 * 2"},
            headers={"Authorization": "Bearer right-key"},
        )
        assert ok.status_code == 202


def test_x_api_key_header_also_works(tmp_path) -> None:
    import hashlib

    keys = {hashlib.sha256(b"k").hexdigest(): "ci"}
    with _client(tmp_path, require_auth=True, api_keys=keys) as client:
        assert client.post(
            "/runs", json={"goal": "Compute 38 * 2"}, headers={"X-API-Key": "k"}
        ).status_code == 202


def test_auth_required_with_no_keys_fails_closed(tmp_path) -> None:
    """A deployment that asked for auth and configured none is broken, not public."""
    with _client(tmp_path, require_auth=True, api_keys={}) as client:
        assert client.post("/runs", json={"goal": "x"}).status_code == 503
        readyz = client.get("/readyz")
        assert readyz.status_code == 503
        assert "no API keys" in readyz.json()["reason"]


def test_liveness_never_requires_auth(tmp_path) -> None:
    """A probe that needs credentials gets the container killed."""
    import hashlib

    keys = {hashlib.sha256(b"k").hexdigest(): "ci"}
    with _client(tmp_path, require_auth=True, api_keys=keys) as client:
        assert client.get("/livez").status_code == 200
        assert client.get("/metrics").status_code == 200


# ── rate limiting ───────────────────────────────────────────────────────


def test_rate_limit_returns_429_with_retry_after(tmp_path) -> None:
    with _client(tmp_path, rate_limit=3) as client:
        codes = [client.get("/runs").status_code for _ in range(6)]
        assert codes[:3] == [200, 200, 200]
        assert 429 in codes
        limited = client.get("/runs")
        assert limited.status_code == 429
        assert "Retry-After" in limited.headers


# ── operations ──────────────────────────────────────────────────────────


def test_health_and_readiness(tmp_path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/livez").json()["status"] == "ok"
        assert client.get("/readyz").json()["ok"] is True
        health = client.get("/healthz").json()
        assert "inflight" in health
        assert "supervisor" in health


def test_metrics_are_prometheus_text(tmp_path) -> None:
    with _client(tmp_path) as client:
        run_id = client.post("/runs", json={"goal": "Compute 38 * 2"}).json()["run_id"]
        _await_run(client, run_id)
        resp = client.get("/metrics")
        assert resp.headers["content-type"].startswith("text/plain")
        assert "forge_runs_total" in resp.text


def test_policy_endpoint_exposes_what_is_denied(tmp_path) -> None:
    with _client(tmp_path) as client:
        policy = client.get("/policy").json()
        assert policy["version"]
        assert policy["capabilities"]["EXTERNAL_PUBLISH"]["granted"] is False
        assert policy["budget"]["max_usd"] > 0


def test_config_endpoint_never_leaks_secrets(tmp_path) -> None:
    with _client(tmp_path) as client:
        body = client.get("/config").text
        for forbidden in ("api_key", "Authorization", "secret", "OPENAI_API_KEY"):
            assert forbidden not in body
