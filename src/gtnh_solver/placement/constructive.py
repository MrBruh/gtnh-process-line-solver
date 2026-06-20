"""placement.constructive - the Phase 1 crude deterministic placer.

First-fit constructive placement on the coarse cell grid: walk machines in input order
(expanding ``count`` into that many instances), and drop each into the first free, in-bounds
slot - scanning the floor layer first, then row by row, then upward - honoring reserved cells
and never overlapping. Orientation is the machine's first listed legal option. No search, no
routing-awareness, no compaction; that is Phase 2 (SA/LNS), see docs/ROADMAP.md.

It returns a :class:`PlacementResult`: either every instance placed, or a partial set plus an
explicit :class:`~gtnh_solver.ir.Infeasibility` naming the machine that did not fit. It never
raises for the expected won't-fit case, matching the validator's report-don't-throw
discipline. The validator independently certifies the result has no overlap / out-of-bounds /
reserved-cell / bad-orientation violations.
"""

from __future__ import annotations

from dataclasses import dataclass

from gtnh_solver.ir import CellBox, CellCoord, Infeasibility, InputIR, Machine, Placement
from gtnh_solver.ir.geometry import Cell, in_region, occupied_cells


@dataclass(frozen=True)
class PlacementResult:
    """Crude placer output: all placements, or a partial set plus why it stalled."""

    placements: tuple[Placement, ...] = ()
    infeasibility: Infeasibility | None = None

    @property
    def ok(self) -> bool:
        """True iff every machine instance was placed."""
        return self.infeasibility is None


def place(problem: InputIR) -> PlacementResult:
    """Deterministically place every machine (expanded by ``count``) into the region."""
    region = problem.bounding_region
    occupied: set[Cell] = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    placements: list[Placement] = []

    for machine in problem.machines:
        orientation = machine.orientation_options[0]
        for instance in range(machine.count):
            origin = _first_fit(machine, region, occupied)
            if origin is None:
                detail = (
                    f"machine {machine.id!r} (instance {instance + 1} of {machine.count}) does "
                    f"not fit in the free space of the {region.sx}x{region.sy}x{region.sz} region"
                )
                return PlacementResult(
                    placements=tuple(placements),
                    infeasibility=Infeasibility(
                        constraint="bounding_region",
                        detail=detail,
                        suggested_relaxation=(
                            "enlarge bounding_region, or remove machines / reserved cells"
                        ),
                    ),
                )
            occupied.update(occupied_cells(origin, machine.footprint))
            placements.append(
                Placement(machine_id=machine.id, cell=origin, orientation=orientation)
            )

    return PlacementResult(placements=tuple(placements))


def _first_fit(machine: Machine, region: CellBox, occupied: set[Cell]) -> CellCoord | None:
    """The first in-bounds, non-overlapping origin for ``machine``, or ``None`` if none fits.

    Scans floor layer first (``y`` outer), then rows (``z``), then columns (``x``), so layouts
    fill the ground before stacking - the buildable-compact bias, crudely.
    """
    for y in range(region.sy):
        for z in range(region.sz):
            for x in range(region.sx):
                origin = CellCoord(x=x, y=y, z=z)
                cells = list(occupied_cells(origin, machine.footprint))
                if all(in_region(c, region) for c in cells) and occupied.isdisjoint(cells):
                    return origin
    return None
