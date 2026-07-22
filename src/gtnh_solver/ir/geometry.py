"""Integer cell-grid geometry shared by both contracts.

Placement and routing run on a **coarse cell grid** (cell = largest common single-block
footprint + routing margin); block accuracy is materialized only at export, never during
search (docs/ARCHITECTURE.md, "Spatial model"). These value types are therefore in *cell*
units, not blocks. Axes follow Minecraft: ``x``/``z`` horizontal, ``y`` vertical (up).
"""

from __future__ import annotations

from collections.abc import Iterator

from pydantic import Field

from ._base import FrozenModel
from .enums import Facing


class CellCoord(FrozenModel):
    """A single cell position on the grid. Frozen so it is hashable (sets / dict keys)."""

    x: int
    y: int
    z: int

    def as_tuple(self) -> Cell:
        """This coord as a bare ``(x, y, z)`` :data:`Cell` tuple - the lightweight form the hot
        grid loops (routing, validation, the build guide) key sets and dicts on."""
        return (self.x, self.y, self.z)


class CellBox(FrozenModel):
    """An axis-aligned box measured in cells, given by its size (each dimension >= 1).

    Used for both a machine ``footprint`` (1x1x1 single-block, or the cell-rounded
    bounding box of a multiblock) and the IR's overall ``bounding_region`` (called
    ``CellBox`` in docs/IR.md - same type here).
    """

    sx: int = Field(default=1, ge=1)
    sy: int = Field(default=1, ge=1)
    sz: int = Field(default=1, ge=1)

    @property
    def volume(self) -> int:
        """Number of cells the box occupies."""
        return self.sx * self.sy * self.sz


# A bare (x, y, z) cell triple - the lightweight form used in hot grid loops, distinct
# from the validated CellCoord value type above.
Cell = tuple[int, int, int]


def occupied_cells(origin: CellCoord, footprint: CellBox) -> Iterator[Cell]:
    """Every cell a footprint box covers, given its minimum-corner ``origin``.

    Conventions shared by placement, router, and validator:
    - ``origin`` is the **minimum corner**; the box occupies
      ``[x, x+sx) x [y, y+sy) x [z, z+sz)``.
    - Orientation-driven rotation of non-cubic footprints is a TODO tied to the dataset
      (1x1x1 machines, the common case, are unaffected). Because this primitive is shared by
      placement, the router AND the validator (its independent safety net), a rotated multi-cell
      machine would be mis-modeled *identically* on both sides - so when rotation lands the
      validator must get its own rotation-aware expansion (or this primitive must be oracle-tested)
      or the gate will share the solver's blind spot instead of catching it. Until then the adapter
      side-steps the blind spot by pinning a non-square-base multiblock to a single orientation
      (``adapter.core._orientations_for``), so every reserved box this expands matches reality; a
      square-base footprint (all current dataset machines) is rotation-invariant and rotates freely.
    """
    for dx in range(footprint.sx):
        for dy in range(footprint.sy):
            for dz in range(footprint.sz):
                yield (origin.x + dx, origin.y + dy, origin.z + dz)


def in_region(cell: Cell, region: CellBox) -> bool:
    """Whether a cell lies inside the origin-anchored bounding region.

    Origin-anchored: ``(x, y, z)`` is in-bounds iff ``0 <= x < sx`` and ``0 <= y < sy`` and
    ``0 <= z < sz``.
    """
    x, y, z = cell
    return 0 <= x < region.sx and 0 <= y < region.sy and 0 <= z < region.sz


# Unit step out of each block face. Minecraft axes: north -z, south +z, east +x, west -x,
# up +y, down -y. Shared by the router (where a port docks) and the validator (face checks).
FACE_DELTAS: dict[Facing, Cell] = {
    Facing.NORTH: (0, 0, -1),
    Facing.SOUTH: (0, 0, 1),
    Facing.EAST: (1, 0, 0),
    Facing.WEST: (-1, 0, 0),
    Facing.UP: (0, 1, 0),
    Facing.DOWN: (0, -1, 0),
}

#: The six unit face-offsets as a bare tuple (``FACE_DELTAS`` values, in face order), for grid
#: neighbour scans that don't need the ``Facing`` key. One source for the router's A* neighbours
#: (``_grid.NEIGHBORS``) and placement's LNS insertion offsets, which both re-derived it.
FACE_OFFSETS: tuple[Cell, ...] = tuple(FACE_DELTAS.values())

#: The face on the far side of a block from a given face (shared by solver + validator for
#: auto-output adjacency: a source's auto-output face meets the target's opposite input face).
OPPOSITE_FACE: dict[Facing, Facing] = {
    Facing.NORTH: Facing.SOUTH,
    Facing.SOUTH: Facing.NORTH,
    Facing.EAST: Facing.WEST,
    Facing.WEST: Facing.EAST,
    Facing.UP: Facing.DOWN,
    Facing.DOWN: Facing.UP,
}


def front_on_boundary(
    origin: CellCoord, footprint: CellBox, front: Facing, region: CellBox
) -> bool:
    """Whether a placed box's front-face plane lies flush on the bounding-region boundary.

    True iff stepping the front-face plane one cell in ``front``'s direction leaves the region -
    the face is up against a region wall (or its floor/ceiling), with no in-region cell in front
    of it. Placement uses this to pin a power source's reserved feed face on the boundary (the
    external power feed enters from outside the structure - docs/DOMAIN.md); the validator
    re-derives the same predicate independently from the occupied cells.
    """
    if front is Facing.NORTH:
        return origin.z == 0
    if front is Facing.SOUTH:
        return origin.z + footprint.sz == region.sz
    if front is Facing.WEST:
        return origin.x == 0
    if front is Facing.EAST:
        return origin.x + footprint.sx == region.sx
    if front is Facing.DOWN:
        return origin.y == 0
    return origin.y + footprint.sy == region.sy  # UP


def auto_output_faces(
    source_origin: CellCoord,
    source_footprint: CellBox,
    source_front: Facing,
    target_origin: CellCoord,
    target_footprint: CellBox,
    target_front: Facing,
) -> tuple[Facing, Facing] | None:
    """The ``(source_face, target_face)`` a source can auto-output across into a target, or None.

    A GT machine ejects into the adjacent block on a usable face; the front face (a machine's
    placement ``orientation``) carries no I/O. So a source auto-feeds a target iff some non-front
    source face touches the target across the opposite face, and that opposite face is not the
    target's own front. Pure geometry on cell *origins* + footprints + the two front faces - it
    takes no IR model types, so it can be shared by the solver (which builds the connection) and
    the placement cost (which rewards orientations that enable one) without either importing the
    other. The validator deliberately re-derives this independently (docs/ARCHITECTURE.md #4).
    """
    source_cells = set(occupied_cells(source_origin, source_footprint))
    target_cells = set(occupied_cells(target_origin, target_footprint))
    for face, (dx, dy, dz) in FACE_DELTAS.items():
        if face is source_front:  # the source's front carries no I/O
            continue
        opposite = OPPOSITE_FACE[face]
        if opposite is target_front:  # the target's input face would be its front
            continue
        if any((x + dx, y + dy, z + dz) in target_cells for x, y, z in source_cells):
            return face, opposite
    return None
