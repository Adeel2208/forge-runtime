"""Structural constraints that the type checker cannot express.

These are cheap and they catch the failure that matters most here: someone
adding a `status` column, or reaching for the database from inside the fold.
Either would end the guarantee that status is derived, and neither would break
a single behavioural test on the day it landed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

KNOWLEDGE = Path(__file__).resolve().parents[2] / "forge" / "knowledge"


def _imports(module: str) -> set[str]:
    tree = ast.parse((KNOWLEDGE / module).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_projection_never_imports_the_store() -> None:
    """The fold takes events as an argument and cannot fetch them itself.

    This is what lets all of history be re-evaluated under a changed policy,
    and what makes the cached projection genuinely disposable.
    """
    assert "forge.knowledge.store" not in _imports("projection.py")


def test_the_projection_performs_no_io() -> None:
    for module in ("projection.py", "policy.py", "lineage.py", "anchors.py", "models.py"):
        imports = _imports(module)
        for forbidden in ("sqlite3", "asyncio", "httpx", "pathlib", "os"):
            assert forbidden not in imports, f"{module} imports {forbidden}"


def test_pure_modules_do_not_import_the_tool_layer() -> None:
    for module in ("projection.py", "policy.py", "lineage.py", "anchors.py"):
        assert "forge.knowledge.tools" not in _imports(module)


@pytest.mark.parametrize(
    "module",
    ["models.py", "events.py", "anchors.py", "lineage.py", "policy.py", "projection.py"],
)
def test_no_module_stores_a_status_field(module: str) -> None:
    """Status is computed. A `status` attribute on a stored model is the wrong turn.

    `NoteState.status` is the projection's *output* and lives in models.py, so
    that one is allowed - what must not appear is a status on `Note` itself or
    a status key in an event payload.
    """
    source = (KNOWLEDGE / module).read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in {"Note", "Anchor", "Attestation"}:
            fields = {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
            assert "status" not in fields, f"{node.name} must not carry a stored status"


def test_no_knowledge_event_payload_carries_a_status() -> None:
    """An event that asserted a status would make the fold a formality."""
    source = (KNOWLEDGE / "events.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "status":
            pytest.fail("a knowledge event payload key named 'status' was found")


def test_every_knowledge_event_type_has_a_builder() -> None:
    """A type nobody can construct is dead weight; one nobody folds is a leak."""
    from forge.knowledge import events as kev
    from forge.knowledge.events import KNOWLEDGE_EVENT_TYPES

    builders = {name for name in dir(kev) if name.startswith("make_")}
    assert len(builders) == len(KNOWLEDGE_EVENT_TYPES)


def test_every_knowledge_event_type_is_handled_by_the_fold() -> None:
    from forge.knowledge.events import KNOWLEDGE_EVENT_TYPES

    source = (KNOWLEDGE / "projection.py").read_text(encoding="utf-8")
    for event_type in KNOWLEDGE_EVENT_TYPES:
        assert f"EventType.{event_type.name}" in source, (
            f"{event_type.name} is appended but never folded"
        )
