"""Every endpoint should be reachable from the interface.

An endpoint no button reaches is either dead weight or a missing feature, and
either way somebody should decide which rather than discover it a year later.
This is a coarse check - it looks for the route's distinctive segment in the
page's JavaScript, not for a working click path - but it catches the case that
actually happens: a capability added to the service and never wired to
anything.

Add a deliberately UI-less endpoint here with a reason if one is ever needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[2] / "forge" / "api"

#: Routes intentionally without a control, and why.
NO_UI_NEEDED: dict[str, str] = {}


def _routes(module: str) -> list[tuple[str, str]]:
    source = (API / module).read_text(encoding="utf-8")
    return re.findall(r'@router\.(get|post|put|delete)\("([^"]+)"\)', source)


def _page(name: str) -> str:
    return (API / name).read_text(encoding="utf-8")


def _reachable(path: str, page: str) -> bool:
    """Does the page appear to call this route?

    Paths are built with template literals - `"/code/tasks/" + id + "/accept"` -
    so the check is for the stable prefix plus the distinctive final segment,
    rather than the route string as written.
    """
    prefix = "/code" + path.split("{")[0].rstrip("/")
    if prefix not in page:
        return False
    if "}" not in path:
        return True
    tail = path.rsplit("}", 1)[-1].strip("/")
    return not tail or f'"{tail}"' in page or f"/{tail}" in page


@pytest.mark.parametrize(("method", "path"), _routes("coding.py"))
def test_every_coding_endpoint_has_a_control(method: str, path: str) -> None:
    key = f"{method.upper()} {path}"
    if key in NO_UI_NEEDED:
        pytest.skip(NO_UI_NEEDED[key])
    assert _reachable(path, _page("workbench.py")), (
        f"{key} is not reachable from Studio. Either wire a control to it, or "
        f"record it in NO_UI_NEEDED with the reason it has none."
    )


def test_the_workbench_offers_the_controls_the_process_needs() -> None:
    """The loop this product exists for: give a task, watch it, read the
    change, decide. Each of those has to be a thing you can press."""
    page = _page("workbench.py")
    for label in ("Give task", "Merge", "Discard", "Stop", "Audit trail", "Review diff"):
        assert label in page, f"no control labelled {label!r}"


def test_a_running_task_can_be_stopped() -> None:
    """Only one task runs at a time, deliberately - so an unstoppable one is a
    blocked application, not merely a slow one."""
    from forge.api.coding import CodingService

    assert hasattr(CodingService, "cancel")
    assert "/cancel" in _page("workbench.py")
