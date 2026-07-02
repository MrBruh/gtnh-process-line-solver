"""placement.constructive - the Phase 1 crude deterministic placer.

First-fit constructive placement on the coarse cell grid: walk machines in **flow order**
(a topological sort by net source->sink, so a producer lands next to its consumer - which lets
the solver auto-feed them without a pipe) and drop each into the first free, in-bounds slot -
scanning the floor layer first, then row by row, then upward - honoring reserved cells and
never overlapping. Orientation is the machine's first listed legal option. One placement per
machine (multi-instance groups are Phase 2 - see ``Machine`` / docs/ROADMAP.md). No search, no
compaction; that is Phase 2 (SA/LNS) too, docs/ROADMAP.md.

A **power source** additionally must sit with its front face flush on the region boundary: the
front is its reserved external-feed face (the builder runs power in from outside the structure -
docs/DOMAIN.md), so first-fit for a source scans for the first slot + orientation that puts the
front on a region wall. The validator enforces the same rule independently.

It returns a :class:`PlacementResult`: either every instance placed, or a partial set plus an
explicit :class:`~gtnh_solver.ir.Infeasibility` naming the machine that did not fit. It never
raises for the expected won't-fit case, matching the validator's report-don't-throw
discipline. The validator independently certifies the result has no overlap / out-of-bounds /
reserved-cell / bad-orientation violations.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    Facing,
    Infeasibility,
    InputIR,
    IODirection,
    Machine,
    Placement,
)
from gtnh_solver.ir.geometry import Cell, front_on_boundary, in_region, occupied_cells


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
        fit = _fit(machine, region, occupied)
        if fit is None:
            return PlacementResult(
                placements=tuple(placements), infeasibility=_wont_fit(machine, region)
            )
        origin, orientation = fit
        occupied.update(occupied_cells(origin, machine.footprint))
        placements.append(Placement(machine_id=machine.id, cell=origin, orientation=orientation))

    return PlacementResult(placements=tuple(placements))


def _wont_fit(machine: Machine, region: CellBox) -> Infeasibility:
    if machine.is_power_source:
        return Infeasibility(
            constraint="power_feed",
            detail=(
                f"power source {machine.id!r} has no free slot with its front (feed) face "
                f"on the boundary of the {region.sx}x{region.sy}x{region.sz} region"
            ),
            suggested_relaxation="enlarge bounding_region, or free cells along its boundary",
        )
    return Infeasibility(
        constraint="bounding_region",
        detail=(
            f"machine {machine.id!r} does not fit in the free space of the "
            f"{region.sx}x{region.sy}x{region.sz} region"
        ),
        suggested_relaxation="enlarge bounding_region, or remove machines / reserved cells",
    )


def _fit(machine: Machine, region: CellBox, occupied: set[Cell]) -> tuple[CellCoord, Facing] | None:
    """The first valid (origin, orientation) for ``machine``, or ``None`` if none exists.

    A normal machine takes the first free origin with its first legal orientation. A power
    source must also put its front face - the reserved external-feed face - flush on the region
    boundary, so it takes the first free origin at which *some* legal orientation does that.
    """
    if not machine.is_power_source:
        origin = _first_fit(machine, region, occupied)
        return None if origin is None else (origin, machine.orientation_options[0])
    for origin in _free_origins(machine, region, occupied):
        for orientation in machine.orientation_options:
            if front_on_boundary(origin, machine.footprint, orientation, region):
                return origin, orientation
    return None


def _first_fit(machine: Machine, region: CellBox, occupied: set[Cell]) -> CellCoord | None:
    """The first in-bounds, non-overlapping origin for ``machine``, or ``None`` if none fits."""
    return next(_free_origins(machine, region, occupied), None)


def _free_origins(machine: Machine, region: CellBox, occupied: set[Cell]) -> Iterator[CellCoord]:
    """Every in-bounds, non-overlapping origin for ``machine``, in first-fit scan order.

    Scans floor layer first (``y`` outer), then rows (``z``), then columns (``x``), so layouts
    fill the ground before stacking - the buildable-compact bias, crudely.
    """
    for y in range(region.sy):
        for z in range(region.sz):
            for x in range(region.sx):
                origin = CellCoord(x=x, y=y, z=z)
                cells = list(occupied_cells(origin, machine.footprint))
                if all(in_region(c, region) for c in cells) and occupied.isdisjoint(cells):
                    yield origin


def _flow_order(problem: InputIR) -> list[Machine]:
    """Machines in producer-before-consumer (topological) order, ties in input order.

    Edges are source-machine -> sink-machine, read from each net's port directions, over the
    **item/fluid** nets only: power is always cabled, never auto-fed, so a power source must not
    wedge itself into the material chain (it would split two machines that should sit adjacent).
    Isolated machines and power sources therefore fall to the end. Cyclic machines also fall back
    to input order. This puts the material chain adjacent so the solver can auto-feed it
    (docs/DOMAIN.md auto-output)."""
    by_id = {m.id: m for m in problem.machines}
    port_dir = {(m.id, p.id): p.direction for m in problem.machines for p in m.faces.ports}
    succ: dict[str, set[str]] = {m.id: set() for m in problem.machines}
    indeg: dict[str, int] = {m.id: 0 for m in problem.machines}
    material: set[str] = set()  # machines tied by an item/fluid net (the chain to keep adjacent)
    for net in problem.nets:
        if net.commodity is Commodity.POWER:
            continue
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
        material.update(sources, sinks)
        for s in sources:
            for t in sinks:
                if s != t and t not in succ[s]:
                    succ[s].add(t)
                    indeg[t] += 1

    # Seed only with material producers (indeg 0 AND in a material net); isolated machines and
    # power sources fall to the end so they never split an auto-feeding chain.
    ready = [m.id for m in problem.machines if indeg[m.id] == 0 and m.id in material]
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
    order += [m.id for m in problem.machines if m.id not in seen]  # cycles, isolated, sources
    return [by_id[i] for i in order]
