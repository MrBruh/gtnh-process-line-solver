"""placement.constructive - the Phase 1 crude deterministic placer.

First-fit constructive placement on the coarse cell grid: walk machines in **flow order**
(a topological sort by net source->sink, so a producer lands next to its consumer - which lets
the solver auto-feed them without a pipe) and drop each into the first free, in-bounds slot -
scanning the floor layer first, then row by row, then upward - honoring reserved cells and
never overlapping. Orientation is the machine's first listed legal option. One placement per
machine (multi-instance groups are Phase 2 - see ``Machine`` / docs/ROADMAP.md). No search, no
compaction; that is Phase 2 (SA/LNS) too, docs/ROADMAP.md.

**The annealer's seed** (``lattice=True``) lays a line of single blocks out on a spaced lattice
instead: rows of ceil(sqrt(n)) blocks with a one-cell channel between neighbours, and a two-cell
aisle between rows. Seeded as the plain row, a single-block line never leaves it: the region is
square and wide, so first-fit lays one row along x, the first move off it doubles the floor area
(which the annealer's starting temperature never accepts), and iron.json's 30 blocks came out as a
one-block-deep wall with its pipes spilling out in front. Seeded packed solid, the anneal ends in a
block the router cannot thread. The lattice is the middle: a near-square floor with routing room
inside it. Each edge of it is held by a whole row or column of blocks, so the floor term barely
moves it and the seed's spacing is what sets the build's size; rows one cell apart instead of two
lost two of eight iron seeds to congestion. The fast path keeps the plain row, whose neighbours
touch and so auto-feed.

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

import math
from collections.abc import Iterator
from dataclasses import dataclass

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    Facing,
    Infeasibility,
    InputIR,
    Machine,
    Placement,
)
from gtnh_solver.ir.geometry import Cell, front_on_boundary, in_region, occupied_cells
from gtnh_solver.ir.nets import net_sources_sinks, port_direction_map


@dataclass(frozen=True)
class PlacementResult:
    """Crude placer output: all placements, or a partial set plus why it stalled."""

    placements: tuple[Placement, ...] = ()
    infeasibility: Infeasibility | None = None

    @property
    def ok(self) -> bool:
        """True iff every machine instance was placed."""
        return self.infeasibility is None


def place(problem: InputIR, *, lattice: bool = False) -> PlacementResult:
    """Deterministically place every machine (one each) into the region.

    ``lattice`` seeds a line of single blocks on the spaced lattice (module docstring); the
    annealer asks for it, the fast path does not. A line with any multiblock ignores it.
    """
    region = problem.bounding_region
    occupied: set[Cell] = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    placements: list[Placement] = []
    window = _lattice_window(problem) if lattice else None

    for machine in _flow_order(problem):
        fit = _fit(machine, region, occupied, window)
        if fit is None:
            return PlacementResult(
                placements=tuple(placements), infeasibility=_wont_fit(machine, region)
            )
        origin, orientation = fit
        occupied.update(occupied_cells(origin, machine.footprint, orientation))
        placements.append(Placement(machine_id=machine.id, cell=origin, orientation=orientation))

    return PlacementResult(placements=tuple(placements))


#: A lattice window's ``(x, z)`` extent in cells, from the region's corner (:func:`_lattice_window`).
_Window = tuple[int, int]

#: Origin strides of the seed lattice along x and z: a one-cell channel between the blocks of a
#: row, a two-cell aisle between rows (module docstring).
_LATTICE_STRIDE_X = 2
_LATTICE_STRIDE_Z = 3


def _lattice_window(problem: InputIR) -> _Window | None:
    """The corner window a single-block line is seeded in, or None to scan plainly.

    Rows of ceil(sqrt(n)) blocks, as many rows as that takes. A line with any multiblock keeps the
    plain scan: its seed is a strip the annealer already folds (#254).
    """
    machines = problem.machines
    if not machines or any(
        (m.footprint.sx, m.footprint.sy, m.footprint.sz) != (1, 1, 1) for m in machines
    ):
        return None
    per_row = math.isqrt(len(machines) - 1) + 1
    rows = -(-len(machines) // per_row)  # ceiling division
    return (per_row - 1) * _LATTICE_STRIDE_X + 1, (rows - 1) * _LATTICE_STRIDE_Z + 1


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
    if machine.outside_front:
        return Infeasibility(
            constraint="outside_front",
            detail=(
                f"{machine.type} {machine.id!r} has no free slot with its front, which faces "
                f"outside the build, on the boundary of the {region.sx}x{region.sy}x{region.sz} "
                f"region"
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


def _fit(
    machine: Machine, region: CellBox, occupied: set[Cell], window: _Window | None = None
) -> tuple[CellCoord, Facing] | None:
    """The first valid (origin, orientation) for ``machine``, or ``None`` if none exists.

    A normal machine takes the first free origin with its first legal orientation. One whose front
    faces outside the build (``Machine.fronts_outside``: a power source's reserved external-feed
    face, a Crop Manager's field) must also put that front flush on the region boundary, so it
    takes the first free origin at which *some* legal orientation does that.
    ``window`` puts the seed lattice's points first (:func:`_scan_origins`).
    """
    if not machine.fronts_outside:
        orientation = machine.orientation_options[0]
        origin = _first_fit(machine, region, occupied, orientation, window)
        return None if origin is None else (origin, orientation)
    # Origin-major, exactly as before. Whether an origin is free now depends on the orientation
    # (a turned non-cubic box covers different cells), so the fit test moves inside the orientation
    # loop rather than filtering origins ahead of it. For a cubic machine - every power source
    # today - the two orders pick the same slot.
    for origin in _scan_origins(region, window):
        for orientation in machine.orientation_options:
            if _fits(machine, origin, orientation, region, occupied) and front_on_boundary(
                origin, machine.footprint, orientation, region
            ):
                return origin, orientation
    return None


def _first_fit(
    machine: Machine,
    region: CellBox,
    occupied: set[Cell],
    orientation: Facing,
    window: _Window | None = None,
) -> CellCoord | None:
    """The first in-bounds, non-overlapping origin for ``machine`` at ``orientation``."""
    return next(_free_origins(machine, region, occupied, orientation, window), None)


def _scan_origins(region: CellBox, window: _Window | None = None) -> Iterator[CellCoord]:
    """Every origin in the region, in first-fit scan order.

    Floor layer first (``y`` outer), then rows (``z``), then columns (``x``), so layouts fill the
    ground before stacking - the buildable-compact bias, crudely. With a lattice ``window``, the
    floor's lattice points inside it come first, in the same row order, then every other origin as
    before, so a machine the lattice cannot take still finds any free slot.
    """
    wx = wz = 0
    if window is not None and region.sy > 0:
        wx, wz = min(window[0], region.sx), min(window[1], region.sz)
        for z in range(0, wz, _LATTICE_STRIDE_Z):
            for x in range(0, wx, _LATTICE_STRIDE_X):
                yield CellCoord(x=x, y=0, z=z)
    for y in range(region.sy):
        for z in range(region.sz):
            for x in range(region.sx):
                on_lattice = not (x % _LATTICE_STRIDE_X or z % _LATTICE_STRIDE_Z)
                if y == 0 and x < wx and z < wz and on_lattice:
                    continue  # a lattice point, already offered
                yield CellCoord(x=x, y=y, z=z)


def _fits(
    machine: Machine,
    origin: CellCoord,
    orientation: Facing,
    region: CellBox,
    occupied: set[Cell],
) -> bool:
    """Whether ``machine`` placed at ``origin`` facing ``orientation`` is in bounds and free."""
    cells = list(occupied_cells(origin, machine.footprint, orientation))
    return all(in_region(c, region) for c in cells) and occupied.isdisjoint(cells)


def _free_origins(
    machine: Machine,
    region: CellBox,
    occupied: set[Cell],
    orientation: Facing,
    window: _Window | None = None,
) -> Iterator[CellCoord]:
    """Every in-bounds, non-overlapping origin for ``machine`` at ``orientation``, in scan order."""
    for origin in _scan_origins(region, window):
        if _fits(machine, origin, orientation, region, occupied):
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
    port_dir = port_direction_map(problem)
    succ: dict[str, set[str]] = {m.id: set() for m in problem.machines}
    indeg: dict[str, int] = {m.id: 0 for m in problem.machines}
    material: set[str] = set()  # machines tied by an item/fluid net (the chain to keep adjacent)
    for net in problem.nets:
        if net.commodity is Commodity.POWER:
            continue
        src_eps, sink_eps = net_sources_sinks(net, port_dir)
        sources = [e.machine_id for e in src_eps]
        sinks = [e.machine_id for e in sink_eps]
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
