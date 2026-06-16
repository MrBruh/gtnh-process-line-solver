"""Integer cell-grid geometry shared by both contracts.

Placement and routing run on a **coarse cell grid** (cell = largest common single-block
footprint + routing margin); block accuracy is materialized only at export, never during
search (docs/ARCHITECTURE.md, "Spatial model"). These value types are therefore in *cell*
units, not blocks. Axes follow Minecraft: ``x``/``z`` horizontal, ``y`` vertical (up).
"""

from __future__ import annotations

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
    ``Box`` in docs/IR.md — same type here).
    """

    sx: int = Field(default=1, ge=1)
    sy: int = Field(default=1, ge=1)
    sz: int = Field(default=1, ge=1)

    @property
    def volume(self) -> int:
        """Number of cells the box occupies."""
        return self.sx * self.sy * self.sz
