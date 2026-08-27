"""Sandbox behaviour, including what it deliberately does not promise.

A sandbox that claims more than it enforces is worse than none, so these tests
assert the *boundaries of the claim* as carefully as the guarantees: the local
tier is asserted NOT to isolate the filesystem, because someone reading the
suite should be able to see exactly where the line is.
"""

from __future__ import annotations

import sys
import time

import pytest

from forge.sandbox import (
    Isolation,
    LocalSandbox,
    SandboxError,
    SandboxLimits,
    SandboxSpec,
    scrub_environment,
    select_sandbox,
)
from tests.conftest import run

PY = sys.executable


def _spec(code: str, cwd, **limit_kwargs):
    return SandboxSpec(
        argv=(PY, "-c", code), cwd=cwd, limits=SandboxLimits(**limit_kwargs)
    )


# ── what it enforces ────────────────────────────────────────────────────


def test_a_command_runs_and_returns_output(tmp_path) -> None:
    result = run(LocalSandbox().run(_spec("print('hello')", tmp_path)))
    assert result.ok
    assert "hello" in result.stdout
    assert result.isolation is Isolation.CONFINED


def test_there_is_no_shell_to_break_out_of(tmp_path) -> None:
    """argv is a list and shell=False, so metacharacters are literal text."""
    marker = tmp_path / "pwned.txt"
    result = run(
        LocalSandbox().run(
            SandboxSpec(
                argv=(PY, "-c", "print('safe')", f"; echo pwned > {marker}"),
                cwd=tmp_path,
            )
        )
    )
    assert result.ok
    assert not marker.exists(), "shell metacharacters must not be interpreted"


def test_a_timeout_kills_the_command(tmp_path) -> None:
    started = time.monotonic()
    result = run(
        LocalSandbox().run(_spec("import time; time.sleep(60)", tmp_path, wall_clock_s=1.5))
    )
    elapsed = time.monotonic() - started

    assert result.timed_out
    assert result.exit_code == 124
    assert elapsed < 20, "the timeout must actually fire, not wait out the sleep"
    assert any("wall_clock_s" in hit for hit in result.limits_hit)


def test_a_spawned_child_is_killed_with_its_parent(tmp_path) -> None:
    """Killing only the direct child leaves orphans holding CPU and locks."""
    psutil = pytest.importorskip("psutil")

    marker = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time, pathlib;"
        f"p = subprocess.Popen([{PY!r}, '-c', 'import time; time.sleep(120)']);"
        f"pathlib.Path({str(marker)!r}).write_text(str(p.pid));"
        "time.sleep(120)"
    )
    result = run(LocalSandbox().run(_spec(code, tmp_path, wall_clock_s=3)))
    assert result.timed_out

    if not marker.exists():
        pytest.skip("child never started")
    child_pid = int(marker.read_text())
    time.sleep(1)
    assert not psutil.pid_exists(child_pid) or not psutil.Process(child_pid).is_running(), \
        "the grandchild survived the tree-kill"


def test_output_is_capped(tmp_path) -> None:
    """A chatty command must not exhaust memory through the pipe."""
    result = run(
        LocalSandbox().run(
            _spec("print('x' * 5_000_000)", tmp_path, max_output_bytes=10_000)
        )
    )
    assert result.truncated
    assert len(result.stdout) <= 10_000


def test_the_working_directory_is_confined(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    sandbox = LocalSandbox(workspace_root=workspace)
    with pytest.raises(SandboxError, match="outside the workspace"):
        run(sandbox.run(_spec("print(1)", outside)))


def test_a_missing_command_is_reported_not_raised(tmp_path) -> None:
    result = run(
        LocalSandbox().run(
            SandboxSpec(argv=("definitely-not-a-real-binary-xyz",), cwd=tmp_path)
        )
    )
    assert result.exit_code == 127
    assert "not found" in result.stderr


# ── secrets ─────────────────────────────────────────────────────────────


def test_secrets_never_reach_the_command(tmp_path) -> None:
    """A command that never sees a token cannot leak, log or commit one."""
    import os

    os.environ["MY_SUPER_SECRET_TOKEN"] = "hunter2"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "aws-secret"
    try:
        result = run(
            LocalSandbox().run(
                _spec("import os; print('|'.join(sorted(os.environ)))", tmp_path)
            )
        )
    finally:
        os.environ.pop("MY_SUPER_SECRET_TOKEN", None)
        os.environ.pop("AWS_SECRET_ACCESS_KEY", None)

    assert result.ok
    assert "SECRET" not in result.stdout.upper()
    assert "hunter2" not in result.stdout
    assert "FORGE_SANDBOX" in result.stdout


def test_scrubbing_is_an_allow_list_not_a_deny_list() -> None:
    """A deny-list loses the first time someone invents a new prefix."""
    scrubbed = scrub_environment(
        base={
            "PATH": "/usr/bin",
            "NOVEL_CREDENTIAL_FORMAT_2031": "secret",
            "COMPANY_INTERNAL_THING": "value",
        }
    )
    assert scrubbed["PATH"] == "/usr/bin"
    assert "NOVEL_CREDENTIAL_FORMAT_2031" not in scrubbed
    assert "COMPANY_INTERNAL_THING" not in scrubbed


def test_explicit_passthrough_is_honoured() -> None:
    scrubbed = scrub_environment(("CI",), base={"PATH": "/bin", "CI": "true"})
    assert scrubbed["CI"] == "true"


def test_passthrough_cannot_smuggle_a_secret() -> None:
    """Even an explicitly allowed name is dropped if it looks like a credential."""
    scrubbed = scrub_environment(
        ("MY_API_KEY",), base={"PATH": "/bin", "MY_API_KEY": "sk-live-123"}
    )
    assert "MY_API_KEY" not in scrubbed


# ── the boundary of the claim ───────────────────────────────────────────


def test_local_tier_does_not_claim_to_contain_hostile_code() -> None:
    """The distinction the whole design rests on, asserted rather than implied."""
    assert Isolation.CONFINED.contains_hostile_code is False
    assert Isolation.CONTAINER.contains_hostile_code is True

    described = LocalSandbox().describe()
    assert "filesystem isolation" in described["does_not_enforce"]
    assert "network isolation" in described["does_not_enforce"]


def test_local_tier_genuinely_does_not_isolate_the_filesystem(tmp_path) -> None:
    """Documented limitation, verified.

    If this ever starts failing, the local tier gained filesystem isolation
    and its advertised guarantees are understating it - which is a good
    problem, but the docs must change with it.
    """
    outside = tmp_path / "outside.txt"
    outside.write_text("visible", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    result = run(
        LocalSandbox(workspace_root=workspace).run(
            _spec(f"print(open({str(outside)!r}).read())", workspace)
        )
    )
    assert "visible" in result.stdout, "CONFINED does not isolate the filesystem"


def test_requesting_network_denial_below_container_is_reported(tmp_path) -> None:
    """Silently ignoring a limit you cannot enforce is how people get hurt."""
    result = run(LocalSandbox().run(_spec("print(1)", tmp_path, network=True)))
    assert any("network" in hit for hit in result.limits_hit)


# ── selection ───────────────────────────────────────────────────────────


def test_selection_returns_the_strongest_available(tmp_path) -> None:
    sandbox = run(select_sandbox(workspace_root=tmp_path))
    assert sandbox.isolation >= Isolation.CONFINED


def test_selection_refuses_rather_than_degrading(tmp_path) -> None:
    """Quietly dropping to a weaker tier is the failure mode to avoid."""
    import shutil

    if shutil.which("docker"):
        pytest.skip("docker is available, so CONTAINER can be satisfied")

    with pytest.raises(SandboxError, match="no sandbox available"):
        run(select_sandbox(workspace_root=tmp_path, minimum=Isolation.CONTAINER))


# ── cost ────────────────────────────────────────────────────────────────


def test_confinement_overhead_is_negligible(tmp_path) -> None:
    """A sandbox people disable because it is slow protects nobody.

    Compares a sandboxed run against a bare `subprocess.run` of the same
    command. The bound is loose because CI machines are noisy; it is here to
    catch an order-of-magnitude regression, not to benchmark.
    """
    import subprocess

    code = "print('x')"
    bare_started = time.monotonic()
    for _ in range(3):
        subprocess.run([PY, "-c", code], capture_output=True, check=False)
    bare = (time.monotonic() - bare_started) / 3

    sandbox = LocalSandbox()
    boxed_started = time.monotonic()
    for _ in range(3):
        run(sandbox.run(_spec(code, tmp_path)))
    boxed = (time.monotonic() - boxed_started) / 3

    assert boxed < bare * 3 + 0.5, (
        f"sandbox overhead too high: {boxed * 1000:.0f}ms vs bare {bare * 1000:.0f}ms"
    )
