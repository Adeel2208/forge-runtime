"""Content-hash anchoring and staleness."""

from __future__ import annotations

from forge.knowledge.anchors import anchor_for, hash_region, is_stale, resolve_region
from tests.knowledge.conftest import SOURCE_V1, SOURCE_V2


def test_a_symbol_anchor_covers_the_whole_indented_block() -> None:
    span = resolve_region(SOURCE_V1, "retry")
    assert span is not None
    start, end = span
    lines = SOURCE_V1.split("\n")
    assert lines[start - 1].startswith("def retry")
    assert 'raise RuntimeError("exhausted")' in lines[end - 1]


def test_an_unchanged_region_is_not_stale() -> None:
    anchor = anchor_for("forge/retry.py", SOURCE_V1, "retry")
    assert anchor is not None
    assert is_stale(anchor, {"forge/retry.py": SOURCE_V1}) is False


def test_changing_the_anchored_function_makes_it_stale() -> None:
    anchor = anchor_for("forge/retry.py", SOURCE_V1, "retry")
    assert anchor is not None
    assert is_stale(anchor, {"forge/retry.py": SOURCE_V2}) is True


def test_editing_elsewhere_in_the_file_does_not_make_it_stale() -> None:
    """Anchoring is to a region, not a file. A note about `retry` survives an
    unrelated edit to the same module - otherwise every note would rot on the
    first commit that touched its file."""
    anchor = anchor_for("forge/retry.py", SOURCE_V1, "retry")
    assert anchor is not None
    edited = SOURCE_V1.replace("import time", "import time\nimport os")
    assert is_stale(anchor, {"forge/retry.py": edited}) is False


def test_a_deleted_file_is_stale() -> None:
    anchor = anchor_for("forge/retry.py", SOURCE_V1, "retry")
    assert anchor is not None
    assert is_stale(anchor, {}) is True


def test_a_deleted_symbol_is_stale() -> None:
    anchor = anchor_for("forge/retry.py", SOURCE_V1, "retry")
    assert anchor is not None
    assert is_stale(anchor, {"forge/retry.py": "def other():\n    pass\n"}) is True


def test_line_span_regions_work() -> None:
    anchor = anchor_for("forge/retry.py", SOURCE_V1, (4, 6))
    assert anchor is not None
    assert is_stale(anchor, {"forge/retry.py": SOURCE_V1}) is False
    assert is_stale(anchor, {"forge/retry.py": SOURCE_V2}) is True


def test_crlf_does_not_make_a_note_stale() -> None:
    """A note written on Windows and checked on Linux must not read as stale.

    This repository has been bitten by platform asymmetry before; a knowledge
    layer that quietly invalidated every note on checkout would be worse than
    no knowledge layer.
    """
    anchor = anchor_for("forge/retry.py", SOURCE_V1, "retry")
    assert anchor is not None
    crlf = SOURCE_V1.replace("\n", "\r\n")
    assert is_stale(anchor, {"forge/retry.py": crlf}) is False


def test_reindenting_is_a_change() -> None:
    """Whitespace inside a line is content: in Python it changes meaning."""
    anchor = anchor_for("forge/retry.py", SOURCE_V1, "retry")
    assert anchor is not None
    reindented = SOURCE_V1.replace("        try:", "            try:")
    assert is_stale(anchor, {"forge/retry.py": reindented}) is True


def test_an_unlocatable_region_has_no_hash() -> None:
    assert hash_region(SOURCE_V1, "nonexistent_symbol") is None
    assert resolve_region(SOURCE_V1, (900, 950)) is None
    assert anchor_for("x.py", SOURCE_V1, "nope") is None


def test_class_and_other_definition_forms_resolve() -> None:
    src = "class Widget:\n    def render(self):\n        return 1\n\n\nx = 2\n"
    span = resolve_region(src, "Widget")
    assert span == (1, 3)
