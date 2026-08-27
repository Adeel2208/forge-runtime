"""Provenance closure: deciding whether a run's evidence is its own.

This is the load-bearing module. If the closure is too small, the store
converges on whatever was written first: a note gets read, the reading run
succeeds, the success is counted as support, and the note promotes itself
through nothing but exposure. If the closure is too large, nothing ever
corroborates and the layer is inert.

The rule: **a run that had X in context and then succeeded is not evidence for
X.** It is evidence that X was not fatal. Those are different claims, and only
the second one is licensed by the observation.

Closure follows three edge kinds, all of which transmit contamination:

  derivation  X derived from Y - reading Y is reading X's ancestry
  merge       X absorbed Y - reading Y is reading what X now says
  collapse    identical bodies written by different runs are one note

Merge edges are followed in **both** directions. Reading the absorbed note
contaminates you with respect to the survivor, and reading the survivor
contaminates you with respect to everything folded into it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from forge.knowledge.models import NoteId

__all__ = ["LineageGraph"]


class LineageGraph:
    """Derivation and merge edges over notes, with a cycle-safe closure."""

    __slots__ = ("_absorbed_by", "_derived_by", "_derived_from", "_survivor_of")

    def __init__(
        self,
        derived_from: Mapping[NoteId, tuple[NoteId, ...]] | None = None,
        merges: Mapping[NoteId, NoteId] | None = None,
    ) -> None:
        """`merges` maps an absorbed note id to the id that survived it."""
        self._derived_from: dict[NoteId, tuple[NoteId, ...]] = dict(derived_from or {})
        self._derived_by: dict[NoteId, set[NoteId]] = {}
        for child, parents in self._derived_from.items():
            for parent in parents:
                self._derived_by.setdefault(parent, set()).add(child)
        self._survivor_of: dict[NoteId, NoteId] = dict(merges or {})
        self._absorbed_by: dict[NoteId, set[NoteId]] = {}
        for absorbed, survivor in self._survivor_of.items():
            self._absorbed_by.setdefault(survivor, set()).add(absorbed)

    # -- construction -------------------------------------------------------

    def add_derivation(self, note_id: NoteId, parents: tuple[NoteId, ...]) -> None:
        if parents:
            self._derived_from[note_id] = parents
            for parent in parents:
                self._derived_by.setdefault(parent, set()).add(note_id)

    def add_merge(self, absorbed: NoteId, survivor: NoteId) -> None:
        """Record that `absorbed` folded into `survivor`.

        Self-merges are ignored rather than rejected: a collapse pass that
        elects a note as its own survivor is a no-op, not an error.
        """
        if absorbed == survivor:
            return
        self._survivor_of[absorbed] = survivor
        self._absorbed_by.setdefault(survivor, set()).add(absorbed)

    # -- queries ------------------------------------------------------------

    def survivor(self, note_id: NoteId) -> NoteId:
        """Follow merge edges to the note that currently carries this content."""
        seen: set[NoteId] = set()
        current = note_id
        while current in self._survivor_of and current not in seen:
            seen.add(current)
            current = self._survivor_of[current]
        return current

    def absorbed_into(self, note_id: NoteId) -> frozenset[NoteId]:
        """Everything that folded into `note_id`, transitively."""
        out: set[NoteId] = set()
        stack = list(self._absorbed_by.get(note_id, ()))
        while stack:
            nid = stack.pop()
            if nid in out:
                continue
            out.add(nid)
            stack.extend(self._absorbed_by.get(nid, ()))
        return frozenset(out)

    def closure(self, seeds: Iterable[NoteId]) -> frozenset[NoteId]:
        """Every note whose content a reader of `seeds` has effectively seen.

        The fixed point over derivation and merge edges, **both traversed in
        both directions**. Cycles terminate: a note already in the set is not
        re-expanded, so a mutually-derived pair closes rather than looping.

        Derivation running downhill is the subtle one. If X derives from Y,
        a run that read only Y is still not independent evidence for X: part
        of what X asserts *is* Y, and the run already had it. Following only
        ancestors would let a note launder itself into independence by being
        restated as a derived note - write Y, derive X from it, and every
        reader of Y becomes a fresh corroborator of X.
        """
        out: set[NoteId] = set()
        stack = list(seeds)
        while stack:
            nid = stack.pop()
            if nid in out:
                continue
            out.add(nid)

            # Derivation, uphill: reading a note is reading what it was built from.
            stack.extend(self._derived_from.get(nid, ()))

            # Derivation, downhill: anything built from what you read carries it.
            stack.extend(self._derived_by.get(nid, ()))

            # Merge, forwards: what this note became.
            survivor = self._survivor_of.get(nid)
            if survivor is not None:
                stack.append(survivor)

            # Merge, backwards: what this note is made of.
            stack.extend(self._absorbed_by.get(nid, ()))

        return frozenset(out)

    def contaminated(self, note_id: NoteId, reader_lineage: Iterable[NoteId]) -> bool:
        """True when a reader of `reader_lineage` cannot be independent of `note_id`.

        Checked against the *survivor* on both sides, so a run that read a note
        which has since been absorbed is still correctly excluded.
        """
        target = self.survivor(note_id)
        seen = self.closure(reader_lineage)
        if target in seen:
            return True
        return any(self.survivor(nid) == target for nid in seen)
