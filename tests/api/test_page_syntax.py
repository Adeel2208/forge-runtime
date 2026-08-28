"""The pages are JavaScript programs living inside Python strings.

That arrangement has one sharp edge and it drew blood twice: a backslash has to
be doubled in the Python source to survive into the page, and when it does not,
the result is a literal newline inside a string literal. Python imports it
happily, the server serves it happily, and the window is simply blank. No test
that exercises the API notices, because the API is fine.

So the pages get parsed. `node` is used when it is present and the tests skip
when it is not, because the suite has to run on a clean checkout with nothing
but pytest - but CI and any machine with the desktop shell will have it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from forge.api.dashboard import DASHBOARD_HTML
from forge.api.pwa import SERVICE_WORKER
from forge.api.workbench import WORKBENCH_HTML

PAGES = {"workbench": WORKBENCH_HTML, "console": DASHBOARD_HTML}


def _scripts(html: str) -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))


def _check(source: str) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(source)
        path = fh.name
    try:
        return subprocess.run(
            ["node", "--check", path], capture_output=True, text=True, timeout=60
        )
    finally:
        Path(path).unlink(missing_ok=True)


needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


@needs_node
@pytest.mark.parametrize("name", sorted(PAGES))
def test_the_page_javascript_parses(name: str) -> None:
    result = _check(_scripts(PAGES[name]))
    assert result.returncode == 0, f"{name} has a syntax error:\n{result.stderr}"


@needs_node
def test_the_service_worker_parses() -> None:
    result = _check(SERVICE_WORKER)
    assert result.returncode == 0, f"service worker has a syntax error:\n{result.stderr}"


@pytest.mark.parametrize("name", sorted(PAGES))
def test_every_element_the_script_reaches_for_exists(name: str) -> None:
    """`$("thing")` against markup that has no `id="thing"` is a null
    dereference at the exact moment the feature is used, and never before."""
    html = PAGES[name]
    ids = set(re.findall(r'id="([\w-]+)"', html))
    wanted = set(re.findall(r'\$\("([\w-]+)"\)', _scripts(html)))
    missing = sorted(wanted - ids)
    assert not missing, f"{name} reads elements that do not exist: {missing}"
