"""Sandboxing: tiered isolation with honest guarantees.

    from forge.sandbox import select_sandbox, SandboxSpec

    sandbox = await select_sandbox(workspace_root=repo)
    result = await sandbox.run(SandboxSpec(argv=("pytest", "-q"), cwd=repo))

The tier is chosen from what the machine actually provides, never assumed, and
`forge doctor` reports which one is active. Policy requires a *minimum* tier
per capability, so a capability that needs containment stays denied on a
machine that cannot provide it - rather than silently running under weaker
confinement than its author intended.
"""

from __future__ import annotations

from pathlib import Path

from forge.sandbox.base import (
    SAFE_ENV_KEYS,
    Isolation,
    Sandbox,
    SandboxError,
    SandboxLimits,
    SandboxResult,
    SandboxSpec,
    scrub_environment,
)
from forge.sandbox.container import DEFAULT_IMAGE, ContainerSandbox
from forge.sandbox.local import LocalSandbox

__all__ = [  # noqa: RUF022 - grouped by concern, not alphabetised
    # contracts
    "Sandbox",
    "Isolation",
    "SandboxSpec",
    "SandboxLimits",
    "SandboxResult",
    "SandboxError",
    # implementations
    "LocalSandbox",
    "ContainerSandbox",
    "DEFAULT_IMAGE",
    # selection
    "select_sandbox",
    "describe_available",
    # helpers
    "scrub_environment",
    "SAFE_ENV_KEYS",
]


async def select_sandbox(
    *,
    workspace_root: Path | None = None,
    minimum: Isolation = Isolation.CONFINED,
    prefer: Isolation = Isolation.CONTAINER,
    image: str = DEFAULT_IMAGE,
) -> Sandbox:
    """Pick the strongest available sandbox, at or above `minimum`.

    Raises `SandboxError` when nothing meets the minimum, rather than quietly
    dropping to a weaker tier. Silently degrading isolation is how a system
    ends up running untrusted code under confinement its author never agreed
    to.
    """
    candidates: list[Sandbox] = []
    if prefer >= Isolation.CONTAINER:
        candidates.append(
            ContainerSandbox(image=image, workspace_root=workspace_root)
        )
    candidates.append(LocalSandbox(workspace_root=workspace_root))

    for candidate in candidates:
        if candidate.isolation < minimum:
            continue
        if await candidate.available():
            return candidate

    raise SandboxError(
        f"no sandbox available at or above {minimum.label}. "
        + (
            "Install Docker to get CONTAINER isolation."
            if minimum >= Isolation.CONTAINER
            else "This should not happen - the local sandbox is always available."
        )
    )


async def describe_available(workspace_root: Path | None = None) -> list[dict[str, object]]:
    """Every tier this machine can offer, for `forge doctor`."""
    out: list[dict[str, object]] = []
    for candidate in (
        ContainerSandbox(workspace_root=workspace_root),
        LocalSandbox(workspace_root=workspace_root),
    ):
        entry = dict(candidate.describe())
        entry["available"] = await candidate.available()
        out.append(entry)
    return out
