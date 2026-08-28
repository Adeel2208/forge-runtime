# Contributing

## Getting set up

```bash
git clone https://github.com/Adeel2208/forge-runtime
cd forge-runtime
pip install -e ".[dev,api]"
pytest
```

The suite needs no services, no network and no API keys — it runs entirely on
`MockProvider` and SQLite. If it needs any of those, that is a bug in the test.

A few tests want `git` and `node` on PATH; they skip cleanly when those are
absent, and CI runs where both are present, so a skip locally is not a pass.

## What CI checks

Everything below runs on every push, and all of it should pass locally first.

| job | what it protects |
|---|---|
| `test` | the suite, `ruff`, and `mypy --strict`, on 3.11/3.12/3.13 and Windows |
| `harness` | the negative fixtures, the case sets, and zero duplicate effects |
| `desktop` | the Electron shell and the served pages parse |
| `package` | the wheel installs clean and ships its data files |
| `docker` | the container builds, boots, and refuses an unauthenticated write |

Windows is in the matrix for a reason: this repository has been bitten twice by
platform asymmetry, and the Windows sandbox path had never executed in CI while
Windows was the primary development platform.

## The things worth knowing before changing anything

**Canonical state is `project(events)`.** There is no authoritative mutable row
anywhere, and a checkpoint is a cache with a watermark. If a change makes state
readable from somewhere other than a fold over the log, it is the wrong change.
See [ADR-0001](docs/adr/0001-event-sourced-canonical-state.md).

**A `Proposal` and an `Action` are different types.** Only the runtime mints an
`Action`, and only with a permit and an idempotency key. "The model's output was
executed directly" should stay unrepresentable rather than merely unlikely.
See [ADR-0005](docs/adr/0005-proposal-and-action-are-different-types.md).

**Recording an effect is the claim on its idempotency key** — one durable append
against a unique index, never read-then-write. That race is the duplicate-effect
bug the benchmark exists to catch.

**Four dependencies.** `pydantic`, `httpx`, `typer`, `PyYAML`. A fifth needs a
reason in an ADR, not a convenience argument.

**The harness must be able to fail.** Negative fixtures — deliberately
non-compliant targets the suite has to fail — are the reason a green result
means anything. A change that makes them pass has broken the harness, not fixed
the fixtures.

## Writing a test

Assert the specific reason, not the general outcome. `status != "CANONICAL"` is
satisfied by a note that failed to promote for entirely the wrong reason, and a
test that cannot tell those apart will one day hide a real defect.

Prefer a test that would have caught the bug you are fixing. Several tests here
exist because something shipped broken and nothing noticed — the page-syntax
check exists because a dropped backslash renders a blank window while every API
test still passes.

## Commit messages

Say what changed and why it was wrong before. If a defect was found by running
the thing rather than reading it, say so — that is the part of the story that
tells the next person where to look.

## Releasing

Releases are tag-driven and deliberate:

```bash
# bump version in pyproject.toml, update CHANGELOG.md
git tag v0.7.0 && git push --tags
```

The tag must match the version in `pyproject.toml`; the release workflow checks
and refuses otherwise. It then builds and publishes the wheel to PyPI through
trusted publishing, and attaches a desktop installer for each platform.

The installers are **not code-signed**. That needs a purchased certificate, and
the release notes say so rather than letting a user discover it through a
SmartScreen warning.
