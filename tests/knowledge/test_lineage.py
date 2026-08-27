"""Lineage closure: the rule that decides whether evidence is independent."""

from __future__ import annotations

from forge.knowledge.lineage import LineageGraph


def test_a_reader_is_contaminated_against_what_it_read() -> None:
    graph = LineageGraph()
    assert graph.contaminated("X", ["X"]) is True


def test_an_unrelated_reader_is_independent() -> None:
    graph = LineageGraph()
    assert graph.contaminated("X", ["Y"]) is False
    assert graph.contaminated("X", []) is False


def test_contamination_runs_uphill_to_ancestors() -> None:
    """Reading X contaminates against X's parents: X contains them."""
    graph = LineageGraph()
    graph.add_derivation("X", ("Y",))
    assert graph.contaminated("Y", ["X"]) is True


def test_contamination_runs_downhill_to_descendants() -> None:
    """Reading Y contaminates against anything derived from Y.

    Without this, a note launders itself into independence by being restated:
    write Y, derive X from it, and every reader of Y becomes a fresh
    corroborator of X.
    """
    graph = LineageGraph()
    graph.add_derivation("X", ("Y",))
    assert graph.contaminated("X", ["Y"]) is True


def test_contamination_is_transitive_through_a_chain() -> None:
    graph = LineageGraph()
    graph.add_derivation("B", ("A",))
    graph.add_derivation("C", ("B",))
    assert graph.contaminated("C", ["A"]) is True
    assert graph.contaminated("A", ["C"]) is True


def test_merge_contaminates_both_ways() -> None:
    graph = LineageGraph()
    graph.add_merge(absorbed="B", survivor="A")
    assert graph.contaminated("A", ["B"]) is True
    assert graph.contaminated("B", ["A"]) is True


def test_survivor_follows_a_merge_chain() -> None:
    graph = LineageGraph()
    graph.add_merge(absorbed="C", survivor="B")
    graph.add_merge(absorbed="B", survivor="A")
    assert graph.survivor("C") == "A"
    assert graph.absorbed_into("A") == frozenset({"B", "C"})


def test_a_derivation_cycle_terminates() -> None:
    """Mutually-derived notes must close, not loop forever."""
    graph = LineageGraph()
    graph.add_derivation("A", ("B",))
    graph.add_derivation("B", ("A",))
    assert graph.closure(["A"]) == frozenset({"A", "B"})


def test_a_merge_cycle_terminates() -> None:
    graph = LineageGraph()
    graph.add_merge(absorbed="A", survivor="B")
    graph.add_merge(absorbed="B", survivor="A")
    assert graph.survivor("A") in {"A", "B"}
    assert graph.closure(["A"]) == frozenset({"A", "B"})


def test_a_self_merge_is_a_no_op() -> None:
    graph = LineageGraph()
    graph.add_merge(absorbed="A", survivor="A")
    assert graph.survivor("A") == "A"
    assert graph.absorbed_into("A") == frozenset()


def test_closure_of_nothing_is_nothing() -> None:
    assert LineageGraph().closure([]) == frozenset()


def test_siblings_sharing_a_parent_contaminate_each_other() -> None:
    """X and Z both derive from Y. A reader of X is not independent of Z.

    This is deliberately aggressive. Two notes built on the same foundation
    share that foundation, so a run that absorbed one has absorbed part of the
    other. ADR-0007 records the cost: broad derivation families corroborate
    slowly. The alternative - counting them as independent - is what lets a
    single seed note manufacture a quorum through its descendants.
    """
    graph = LineageGraph()
    graph.add_derivation("X", ("Y",))
    graph.add_derivation("Z", ("Y",))
    assert graph.contaminated("Z", ["X"]) is True
