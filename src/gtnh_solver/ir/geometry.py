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


class CellCoord(FrozenModel):
    """A single cell position on the grid. Frozen so it is hashable (sets / dict keys)."""

    x: int
    y: int
    z: int


class CellBox(FrozenModel):
    """An axis-aligned box measured in cells, given by its size (each dimension >= 1).

    Used for both a machine ``footprint`` (1x1x1 single-block, or the cell-rounded
    bounding box of a multiblock) and the IR's overall ``bounding_region`` (called
    ``Box`` in docs/IR.md - same type here).
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
      (1x1x1 machines, the common case, are unaffected).
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
