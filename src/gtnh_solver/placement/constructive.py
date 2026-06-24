"""placement.constructive - the Phase 1 crude deterministic placer.

First-fit constructive placement on the coarse cell grid: walk machines in **flow order**
(a topological sort by net source->sink, so a producer lands next to its consumer - which lets
the solver auto-feed them without a pipe) and drop each into the first free, in-bounds slot -
scanning the floor layer first, then row by row, then upward - honoring reserved cells and
never overlapping. Orientation is the machine's first listed legal option. One placement per
machine (multi-instance groups are Phase 2 - see ``Machine`` / docs/ROADMAP.md). No search, no
compaction; that is Phase 2 (SA/LNS) too, docs/ROADMAP.md.

It returns a :class:`PlacementResult`: either every instance placed, or a partial set plus an
explicit :class:`~gtnh_solver.ir.Infeasibility` naming the machine that did not fit. It never
raises for the expected won't-fit case, matching the validator's report-don't-throw
discipline. The validator independently certifies the result has no overlap / out-of-bounds /
reserved-cell / bad-orientation violations.
"""

from __future__ import annotations

from dataclasses import dataclass

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Infeasibility,
    InputIR,
    IODirection,
    Machine,
    Placement,
)
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
    """Deterministically place every machine (one each) into the region."""
    region = problem.bounding_region
    occupied: set[Cell] = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    placements: list[Placement] = []

    for machine in _flow_order(problem):
        orientation = machine.orientation_options[0]
        origin = _first_fit(machine, region, occupied)
        if origin is None:
            detail = (
                f"machine {machine.id!r} does not fit in the free space of the "
                f"{region.sx}x{region.sy}x{region.sz} region"
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
        placements.append(Placement(machine_id=machine.id, cell=origin, orientation=orientation))

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


def _flow_order(problem: InputIR) -> list[Machine]:
    """Machines in producer-before-consumer (topological) order, ties in input order.

    Edges are source-machine -> sink-machine, read from each net's port directions. Cyclic /
    unreachable machines fall back to input order at the end. This puts connected machines
    adjacent so the solver can auto-feed them (docs/DOMAIN.md auto-output)."""
    by_id = {m.id: m for m in problem.machines}
    port_dir = {(m.id, p.id): p.direction for m in problem.machines for p in m.faces.ports}
    succ: dict[str, set[str]] = {m.id: set() for m in problem.machines}
    indeg: dict[str, int] = {m.id: 0 for m in problem.machines}
    for net in problem.nets:
        sources = [
            e.machine_id
            for e in net.endpoints
            if port_dir.get((e.machine_id, e.port_id)) is IODirection.OUTPUT
        ]
        sinks = [
            e.machine_id
            for e in net.endpoints
            if port_dir.get((e.machine_id, e.port_id)) is IODirection.INPUT
        ]
        for s in sources:
            for t in sinks:
                if s != t and t not in succ[s]:
                    succ[s].add(t)
                    indeg[t] += 1

    ready = [m.id for m in problem.machines if indeg[m.id] == 0]
    order: list[str] = []
    seen: set[str] = set()
    while ready:
        nid = ready.pop(0)
        seen.add(nid)
        order.append(nid)
        for t in sorted(succ[nid]):
            indeg[t] -= 1
            if indeg[t] == 0:
                ready.append(t)
    order += [m.id for m in problem.machines if m.id not in seen]  # cycles / leftovers
    return [by_id[i] for i in order]
