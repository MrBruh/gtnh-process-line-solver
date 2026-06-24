"""Cell-graph connectivity for route validation.

The cell-grid primitives (``Cell``, ``occupied_cells``, ``in_region``) live in
``gtnh_solver.ir.geometry`` next to the value types they operate on, and are re-exported here
so the validator's imports stay in one place. The origin-anchored / minimum-corner conventions
are documented there. This module adds only ``is_connected`` (route-graph specific).
"""

from __future__ import annotations

from collections.abc import Iterable

from gtnh_solver.ir.geometry import FACE_DELTAS, OPPOSITE_FACE, Cell, in_region, occupied_cells

__all__ = [
    "FACE_DELTAS",
    "OPPOSITE_FACE",
    "Cell",
    "in_region",
    "is_connected",
    "is_unit_step",
    "occupied_cells",
]


def is_unit_step(a: Cell, b: Cell) -> bool:
    """Whether ``a`` and ``b`` are exactly one axis-aligned cell apart (a legal single hop).

    A :class:`~gtnh_solver.ir.Segment` must be a unit Manhattan step; connectivity alone does
    not catch a single segment that "teleports" two cells (or diagonally) across a machine -
    that route would still be one connected component. The validator checks every segment with
    this so such a jump is rejected, not certified.
    """
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2]) == 1


def is_connected(edges: Iterable[tuple[Cell, Cell]]) -> bool:
    """Whether the cell graph formed by ``edges`` is a single connected component.

    Handles trees (a power route serving several machines is Steiner-tree-like), not just
    simple paths. An empty edge set is *not* connected - a routed net needs at least one hop.
    """
    parent: dict[Cell, Cell] = {}

    def find(a: Cell) -> Cell:
        root = a
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(a, a) != root:  # path compression
            parent[a], a = root, parent[a]
        return root

    nodes: set[Cell] = set()
    saw_edge = False
    for a, b in edges:
        saw_edge = True
        nodes.add(a)
        nodes.add(b)
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        parent[find(a)] = find(b)

    if not saw_edge:
        return False
    return len({find(n) for n in nodes}) == 1
