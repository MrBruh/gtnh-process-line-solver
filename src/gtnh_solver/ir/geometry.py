"""Integer cell-grid geometry shared by both contracts.

Placement and routing run on a **coarse cell grid** (cell = largest common single-block
footprint + routing margin); block accuracy is materialized only at export, never during
search (docs/ARCHITECTURE.md, "Spatial model"). These value types are therefore in *cell*
units, not blocks. Axes follow Minecraft: ``x``/``z`` horizontal, ``y`` vertical (up).
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import cache

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


#: Clockwise quarter-turns about +Y that take NORTH to each horizontal facing. The multiblock dump
#: records every machine at ``controller front = NORTH (-Z)``, so this is the yaw its contents take
#: when the placer faces the machine some other way. A vertical facing carries no yaw and reads as
#: 0: ``orientation`` comes from ``Machine.orientation_options``, which the adapter fills with
#: horizontals only, so a vertical one means the caller passed a face where a facing belongs.
CW_STEPS: dict[Facing, int] = {
    Facing.NORTH: 0,
    Facing.EAST: 1,
    Facing.SOUTH: 2,
    Facing.WEST: 3,
}


def rotate_offset(dx: int, dz: int, steps: int) -> tuple[int, int]:
    """Rotate a horizontal offset by ``steps`` clockwise quarter-turns, viewed from +Y.

    One step sends NORTH ``(0, -1)`` to EAST ``(1, 0)``. The general primitive: a footprint box
    only needs its extents swapped (:func:`rotated_footprint`), but a hatch slot's offset is an
    arbitrary point and needs this.
    """
    for _ in range(steps % 4):
        dx, dz = -dz, dx
    return dx, dz


@cache
def _swapped(sx: int, sy: int, sz: int) -> CellBox:
    """The horizontal-extent swap, memoized on plain ints.

    Keyed on ints rather than on the ``CellBox`` itself: this sits under ``occupied_cells``, which a
    single solve calls hundreds of thousands of times, and hashing a pydantic model for the cache
    lookup costs more than the box construction it saves.
    """
    return CellBox(sx=sz, sy=sy, sz=sx)


def rotated_footprint(footprint: CellBox, orientation: Facing) -> CellBox:
    """``footprint`` as it sits in the world facing ``orientation``.

    A quarter-turn swaps the horizontal extents; y is a rotation axis and never moves. Rotating a
    *box* needs nothing more than this, because the box is symmetric about its own centre: rotating
    every cell and re-anchoring the minimum corner (what :func:`rotate_offset` does for an arbitrary
    offset set) yields exactly the swapped extents. A property test pins that equivalence, since
    lane 2's hatch slots take the general path and the two must not drift.

    Two cheap outs come first, and between them they cover every machine in both shipped examples:
    a square base is rotation-invariant, and a half turn restores the extents.
    """
    if footprint.sx == footprint.sz or CW_STEPS.get(orientation, 0) % 2 == 0:
        return footprint
    return _swapped(footprint.sx, footprint.sy, footprint.sz)


def occupied_cells(origin: CellCoord, footprint: CellBox, orientation: Facing) -> Iterator[Cell]:
    """Every cell a footprint box covers, given its minimum-corner ``origin`` and its facing.

    Conventions shared by placement, router, and validator:
    - ``origin`` is the **minimum corner**; the box occupies
      ``[x, x+sx) x [y, y+sy) x [z, z+sz)`` of the *rotated* footprint.
    - Rotation preserves the minimum-corner anchor: the cells turn about +Y and are then
      re-anchored so their minimum corner is still ``origin``. Anything else would move every
      placement, since ``origin`` is what a ``Placement`` records.

    ``orientation`` is required rather than defaulted on purpose. This primitive is shared by
    placement, the router AND the validator's independent safety net, so a caller that forgot to
    rotate would be wrong *identically* on both sides - the one failure class
    docs/ARCHITECTURE.md #4 exists to prevent, and one that fails silently (a plausible layout that
    validates clean and cannot be built). Making it required turns every such caller into a type
    error instead. The validator does not use this function at all; it expands independently
    (``validator/_geometry.body_cells``).
    """
    box = rotated_footprint(footprint, orientation)
    for dx in range(box.sx):
        for dy in range(box.sy):
            for dz in range(box.sz):
                yield (origin.x + dx, origin.y + dy, origin.z + dz)


def rotated_slot(offset: Cell, footprint: CellBox, orientation: Facing) -> Cell:
    """Where a slot sits inside its own machine once the machine faces ``orientation``.

    ``offset`` is relative to the machine's **unrotated** minimum corner, which is how
    ``Machine.hatch_slots`` records it; the result is relative to the *rotated* minimum corner, so
    ``placement.cell + rotated_slot(...)`` is the slot's world cell and always lands inside
    ``occupied_cells(placement.cell, footprint, orientation)``.

    The re-anchoring is the part that is easy to get wrong. Turning the box moves its minimum
    corner, so a rotated offset has to be shifted back by however far the corner travelled: a
    quarter turn clockwise sends ``x`` to ``-z``, whose smallest value over a box of depth ``sz`` is
    ``-(sz - 1)``. A property test pins that this maps the box's own cells exactly onto
    ``occupied_cells``, which is the same equivalence :func:`rotated_footprint` relies on.
    """
    dx, dy, dz = offset
    steps = CW_STEPS.get(orientation, 0) % 4
    x, z = rotate_offset(dx, dz, steps)
    if steps == 1:
        x += footprint.sz - 1
    elif steps == 2:
        x += footprint.sx - 1
        z += footprint.sz - 1
    elif steps == 3:
        z += footprint.sx - 1
    return (x, dy, z)


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
    # The depth to step is the ROTATED extent: an east-facing 5x1x2 is 2 deep along x, not 5.
    # ``front`` is the orientation, so the box is measured as it actually sits in the world.
    box = rotated_footprint(footprint, front)
    if front is Facing.NORTH:
        return origin.z == 0
    if front is Facing.SOUTH:
        return origin.z + box.sz == region.sz
    if front is Facing.WEST:
        return origin.x == 0
    if front is Facing.EAST:
        return origin.x + box.sx == region.sx
    if front is Facing.DOWN:
        return origin.y == 0
    return origin.y + box.sy == region.sy  # UP


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

    Answered from the two rotated boxes, never from their cells. Both bodies are solid axis-aligned
    boxes, so "some source cell has a neighbour across ``face`` inside the target" is exactly "the
    source box stepped one cell along ``face`` overlaps the target box on all three axes" - six
    integer comparisons, independent of machine volume. Enumerating instead made this 70% of a
    solve (issue #110): the placement loop calls it ~1.5M times, and a 7x7x7 multiblock is 343
    cells to build and hash on every one of them. A property test pins this against the cell-set
    formulation over random boxes, origins and orientations.
    """
    source_box = rotated_footprint(source_footprint, source_front)
    target_box = rotated_footprint(target_footprint, target_front)
    # Hoisted out of the loop: the same coordinates are re-read on every candidate face, and at this
    # call count the attribute lookups are a measurable share of what is left.
    ox, oy, oz = source_origin.x, source_origin.y, source_origin.z
    tx, ty, tz = target_origin.x, target_origin.y, target_origin.z
    tx_max, ty_max, tz_max = tx + target_box.sx, ty + target_box.sy, tz + target_box.sz
    for face, (dx, dy, dz) in FACE_DELTAS.items():
        if face is source_front:  # the source's front carries no I/O
            continue
        opposite = OPPOSITE_FACE[face]
        if opposite is target_front:  # the target's input face would be its front
            continue
        # The stepped source box's minimum corner; half-open intervals on both sides, so the test
        # is min < other_max on each axis in both directions.
        ax, ay, az = ox + dx, oy + dy, oz + dz
        if (
            ax < tx_max
            and tx < ax + source_box.sx
            and ay < ty_max
            and ty < ay + source_box.sy
            and az < tz_max
            and tz < az + source_box.sz
        ):
            return face, opposite
    return None
