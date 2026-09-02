"""Cell-graph connectivity and the validator's own geometry, for route validation.

**This module deliberately does not use the solver's cell expansion.** It used to re-export
``ir.geometry.occupied_cells``, which was harmless only while that function could not rotate: the
moment a footprint turns, a bug in it would be mis-modelled *identically* in the solver and in the
only automated gate that exists to catch solver bugs, and it would fail silently - a plausible
layout, on the wrong cells, validating clean. That is the failure docs/ARCHITECTURE.md #4 exists to
prevent, so :func:`body_cells` below is written from the dataset's stated convention rather than
derived from the solver's code.

What stays shared is *data*, not derivation: ``FACE_DELTAS`` and ``OPPOSITE_FACE`` are six unit
vectors and six pairs, and sharing them keeps the two sides' notion of "north" from drifting the
way ``validator/core`` already argues for ``tier_voltage`` and ``CABLE_LOSS_PER_BLOCK``. ``Cell`` is
a type alias. Everything computed from them is computed here.
"""

from __future__ import annotations

from collections.abc import Iterable

from gtnh_solver.ir import CellBox, CellCoord, Facing, HatchSlot
from gtnh_solver.ir.geometry import FACE_DELTAS, OPPOSITE_FACE, Cell

__all__ = [
    "FACE_DELTAS",
    "OPPOSITE_FACE",
    "Cell",
    "body_cells",
    "hatch_cells",
    "in_region",
    "is_connected",
    "is_unit_step",
]

#: How many quarter-turns each facing is from NORTH, counted the way the dump states its
#: convention: *controller front = NORTH (-Z)*, and a machine faced elsewhere is that structure
#: turned about the vertical axis. Derived here from the convention rather than imported, so a
#: mistake in the solver's table cannot hide in both places at once.
_QUARTER_TURNS_FROM_NORTH: dict[Facing, int] = {
    Facing.NORTH: 0,
    Facing.EAST: 1,
    Facing.SOUTH: 2,
    Facing.WEST: 3,
}


def body_cells(origin: CellCoord, footprint: CellBox, orientation: Facing) -> set[Cell]:
    """The world cells a machine placed at ``origin`` facing ``orientation`` occupies.

    The validator's own expansion (see the module docstring). Written from the convention: turning
    a structure a quarter turn about the vertical axis exchanges its two horizontal extents and
    leaves its height alone, and ``origin`` names the minimum corner of the box *as placed*, so the
    turned box is measured from the same corner.

    Returns a set because every caller here asks a membership question; none needs an order.
    """
    turns = _QUARTER_TURNS_FROM_NORTH.get(orientation, 0)
    width, depth = (footprint.sz, footprint.sx) if turns % 2 else (footprint.sx, footprint.sz)
    return {
        (origin.x + dx, origin.y + dy, origin.z + dz)
        for dx in range(width)
        for dy in range(footprint.sy)
        for dz in range(depth)
    }


def hatch_cells(
    origin: CellCoord, footprint: CellBox, orientation: Facing, slots: Iterable[HatchSlot]
) -> dict[Cell, tuple[str, ...]]:
    """Where each recorded hatch slot lands once the machine is placed and turned, to its kinds.

    The validator's own re-derivation, from the convention rather than from
    ``ir.geometry.rotated_slot`` (see the module docstring). A slot offset is measured from the
    machine's **unrotated** minimum corner, so it turns with the structure and is then re-anchored,
    exactly as the body box is.

    Working it out from the convention alone: the dump's front is NORTH, which is ``-Z``, so facing
    the machine EAST is the turn that carries ``-Z`` onto ``+X``, and that turn sends an offset
    ``(dx, dz)`` to ``(-dz, dx)``. That lands the turned offsets in ``-(sz - 1) .. 0`` on x, so
    adding ``sz - 1`` puts the minimum corner back at ``origin``. Two and three turns are the same
    map applied again, with the extents that turn exchanged. Height is untouched: the turn is about
    the vertical axis.

    Two slots can never land on one cell (the turn is a bijection), so a dict loses nothing; a
    machine that repeats an offset in its dump would collapse, which is a dump defect either way.
    """
    turns = _QUARTER_TURNS_FROM_NORTH.get(orientation, 0)
    sx, sz = footprint.sx, footprint.sz
    placed: dict[Cell, tuple[str, ...]] = {}
    for slot in slots:
        dx, dy, dz = slot.offset.as_tuple()
        if turns == 1:
            x, z = sz - 1 - dz, dx
        elif turns == 2:
            x, z = sx - 1 - dx, sz - 1 - dz
        elif turns == 3:
            x, z = dz, sx - 1 - dx
        else:
            x, z = dx, dz
        placed[(origin.x + x, origin.y + dy, origin.z + z)] = slot.kinds
    return placed


def in_region(cell: Cell, region: CellBox) -> bool:
    """Whether a cell lies inside the origin-anchored bounding region.

    Origin-anchored: ``(x, y, z)`` is in-bounds iff ``0 <= x < sx``, ``0 <= y < sy`` and
    ``0 <= z < sz``. A derivation, so the validator keeps its own copy.
    """
    x, y, z = cell
    return 0 <= x < region.sx and 0 <= y < region.sy and 0 <= z < region.sz


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
