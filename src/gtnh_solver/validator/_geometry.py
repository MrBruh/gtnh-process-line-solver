"""Pure cell-grid geometry helpers for the validator.

Conventions (must match placement/router when they land):
- The bounding region is anchored at the origin: a cell ``(x, y, z)`` is in-bounds iff
  ``0 <= x < sx`` and ``0 <= y < sy`` and ``0 <= z < sz``.
- A ``Placement.cell`` is the **minimum corner** of the machine's footprint box; the machine
  occupies ``[x, x+sx) x [y, y+sy) x [z, z+sz)``. Orientation-driven rotation of non-cubic
  footprints is a TODO tied to the dataset (1x1x1 machines, the common case, are unaffected).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from gtnh_solver.ir import CellBox, CellCoord

Cell = tuple[int, int, int]


def occupied_cells(origin: CellCoord, footprint: CellBox) -> Iterator[Cell]:
    """Every cell a footprint box covers, given its minimum-corner ``origin``."""
    for dx in range(footprint.sx):
        for dy in range(footprint.sy):
            for dz in range(footprint.sz):
                yield (origin.x + dx, origin.y + dy, origin.z + dz)


def in_region(cell: Cell, region: CellBox) -> bool:
    """Whether a cell lies inside the origin-anchored bounding region."""
    x, y, z = cell
    return 0 <= x < region.sx and 0 <= y < region.sy and 0 <= z < region.sz


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
