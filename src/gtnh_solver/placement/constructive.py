"""placement.constructive - the Phase 1 crude deterministic placer.

First-fit constructive placement on the coarse cell grid: walk machines in **flow order**
(a topological sort by net source->sink, so a producer lands next to its consumer - which lets
the solver auto-feed them without a pipe) and drop each into the first free, in-bounds slot -
scanning the floor layer first, then row by row, then upward - honoring reserved cells and
never overlapping. Orientation is the machine's first listed legal option. One placement per
machine (multi-instance groups are Phase 2 - see ``Machine`` / docs/ROADMAP.md). No search, no
compaction; that is Phase 2 (SA/LNS) too, docs/ROADMAP.md.

**The annealer's seed** (``lattice=True``) spaces the line out instead. Each machine is offered
some origins first, then the plain scan as before, so a machine its offer cannot take still finds
any free slot::

    place(lattice=True):  offers  -->  fit each machine in flow order, offered origins first,
                                       then the plain scan; lift a busy single block
                                  -->  anything left unplaced?  -->  place() plain, from scratch

    single blocks: the lattice              a line with a multiblock: shelves
    (every machine offered every point)     (each machine offered its own slot)

         x0  x2  x4                              x0      x4      x8
    z0   a . b . c      . one-cell channel  z0   B B B . c . D D . e     |<- width W ->|
         . . . . .                               B B B . . . D D
         . . . . .      two-cell aisle           B B B . . . . .
    z3   d . e . f                               . . . . . . . . . .     aisle behind the
         . . . . .                               . . . . . . . . . .     row's deepest machine
         . . . . .                          z5   F F . g . h . . .
    z6   g . h

A line of **single blocks** goes on a lattice: rows of ceil(sqrt(n)) blocks with a one-cell
channel between neighbours and a two-cell aisle between rows, every machine taking the first free
point. Seeded as the plain row, it never leaves it: the region is square and wide, so first-fit
lays one row along x, the first move off it doubles the floor area (which the annealer's starting
temperature never accepts), and iron.json's 30 blocks came out as a one-block-deep wall with its
pipes spilling out in front. Seeded packed solid, the anneal ends in a block the router cannot
thread. The lattice is the middle: a near-square floor with routing room inside it. Each edge of
it is held by a whole row or column of blocks, so the floor term barely moves it and the seed's
spacing is what sets the build's size; rows one cell apart instead of two lost two of eight iron
seeds to congestion.

A line **with any multiblock** goes on shelves, the same spacing for boxes of any size: walk the
machines in flow order, each at its first orientation, a channel after each one, and start a new
row behind the deepest machine of the last (and an aisle) once the next one would cross the shelf
width W, about the square root of the floor the spaced boxes need. Each machine is offered only its
own slot, and one that would cross the region's far edge is offered none. The lattice is the shelf
of unit blocks, with W set by the count instead, which is what #272 measured. Seeded as the plain
row, log-bug's 104 machines lay out 168 cells long and one deep, and the annealer never folded
it: the full search finished a 150x6x6 strip with 7 machines boxed in by their neighbours
(``feasibility.crowded_machines``), where the shelves start it with 4 crowded instead of 22.

**A busy single block is lifted off the floor.** On the floor a single block loses its down face,
and its front carries no I/O, so it has four faces left for its connections: with five it can
never dock them all there, and with four it has no face to spare for a neighbour. Such blocks are
most of what the crowding gate (``feasibility.crowded_machines``) still finds in a spaced seed. So
in the spaced seed a single block with ``_LIFT_CONNECTIONS`` (four) or more connections takes its
slot as before, then moves one cell up, and the slot below it is held empty for its down face.
Every other machine lands where it would have::

    side view of a row      y1   . . B . .      B: a busy block, its down face now usable
                            y0   a . _ . c      _: its slot, held empty

On the community plans that leaves bio-diesel's and log-bug's seeds with no machine crowded instead
of 8 and 4, and platline's with 1 instead of 14. Lifting only blocks with five connections left 2,
2 and 3, and solved worse on every line it changed (``search`` has the numbers).

**The spaced seed never costs a line its feasibility.** Offers can strand a machine the plain scan
would have seated (an early one taking cells a later, bigger one needed), so if anything is left
unplaced the whole seed is laid again by the plain scan. The solver stops on a seed it cannot
place, so a line the plain scan places is never refused because it was spaced. The fallback lifts
nothing, and neither does the fast path, which keeps the plain row, whose neighbours touch and so
auto-feed.

A **power source** additionally must sit with its front face flush on the region boundary: the
front is its reserved external-feed face (the builder runs power in from outside the structure -
docs/DOMAIN.md), so first-fit for a source scans for the first slot + orientation that puts the
front on a region wall. The validator enforces the same rule independently. In the spaced seed it
takes an offered slot only where that holds, and the plain boundary scan otherwise.

It returns a :class:`PlacementResult`: either every instance placed, or a partial set plus an
explicit :class:`~gtnh_solver.ir.Infeasibility` naming the machine that did not fit. It never
raises for the expected won't-fit case, matching the validator's report-don't-throw
discipline. The validator independently certifies the result has no overlap / out-of-bounds /
reserved-cell / bad-orientation violations.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterator, Mapping, Sequence
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
from gtnh_solver.ir.geometry import (
    Cell,
    front_on_boundary,
    in_region,
    occupied_cells,
    rotated_footprint,
)
from gtnh_solver.ir.nets import connection_counts, net_sources_sinks, port_direction_map


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

    ``lattice`` asks for the annealer's spaced seed (module docstring): single blocks on the
    lattice, a line with a multiblock on shelves, busy single blocks lifted off the floor, and the
    plain scan if any of that strands a machine. The annealer asks for it, the fast path does not.
    """
    order = _flow_order(problem)
    if lattice:
        spaced = _place(problem, order, _seed_offers(problem, order), _busy_blocks(problem))
        if spaced.ok:
            return spaced
    return _place(problem, order, {})


def _place(
    problem: InputIR,
    order: Sequence[Machine],
    offers: Mapping[str, Sequence[CellCoord]],
    lift: Collection[str] = frozenset(),
) -> PlacementResult:
    """Fit each machine of ``order`` in turn, its ``offers`` origins first (:func:`_fit`), and
    raise the ones in ``lift`` off the floor once they have their slot (:func:`_lifted`)."""
    region = problem.bounding_region
    occupied: set[Cell] = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    placements: list[Placement] = []

    for machine in order:
        fit = _fit(machine, region, occupied, offers.get(machine.id, ()))
        if fit is None:
            return PlacementResult(
                placements=tuple(placements), infeasibility=_wont_fit(machine, region)
            )
        origin, orientation = fit
        if machine.id in lift:
            origin = _lifted(machine, origin, orientation, region, occupied)
        occupied.update(occupied_cells(origin, machine.footprint, orientation))
        placements.append(Placement(machine_id=machine.id, cell=origin, orientation=orientation))

    return PlacementResult(placements=tuple(placements))


#: The gaps of the spaced seed: a one-cell channel between neighbours in a row, a two-cell aisle
#: between rows (module docstring).
_CHANNEL = 1
_AISLE = 2
#: Origin strides of the single-block lattice along x and z: a unit block plus the gap after it.
_LATTICE_STRIDE_X = 1 + _CHANNEL
_LATTICE_STRIDE_Z = 1 + _AISLE


def _seed_offers(problem: InputIR, order: Sequence[Machine]) -> dict[str, tuple[CellCoord, ...]]:
    """The origins each machine is offered first in the spaced seed: every lattice point for a
    line of single blocks, or its own shelf slot for a line with a multiblock."""
    if all(m.footprint.volume == 1 for m in order):
        points = _lattice_points(problem.bounding_region, len(order))
        return dict.fromkeys((m.id for m in order), points)
    return _shelf_slots(problem.bounding_region, order)


def _lattice_points(region: CellBox, count: int) -> tuple[CellCoord, ...]:
    """The floor points a line of ``count`` single blocks is seeded on, row by row.

    Rows of ceil(sqrt(n)) blocks, as many rows as that takes, clipped to the region.
    """
    if count == 0:
        return ()
    per_row = math.isqrt(count - 1) + 1
    rows = -(-count // per_row)  # ceiling division
    wx = min((per_row - 1) * _LATTICE_STRIDE_X + 1, region.sx)
    wz = min((rows - 1) * _LATTICE_STRIDE_Z + 1, region.sz)
    return tuple(
        CellCoord(x=x, y=0, z=z)
        for z in range(0, wz, _LATTICE_STRIDE_Z)
        for x in range(0, wx, _LATTICE_STRIDE_X)
    )


def _shelf_slots(region: CellBox, order: Sequence[Machine]) -> dict[str, tuple[CellCoord, ...]]:
    """Each machine's shelf slot, walking ``order`` at each machine's first orientation.

    A machine that would cross the region's far edge gets none, and is seated by the plain scan.
    """
    boxes = [(m.id, rotated_footprint(m.footprint, m.orientation_options[0])) for m in order]
    width = _shelf_width(region, [box for _, box in boxes])
    slots: dict[str, tuple[CellCoord, ...]] = {}
    x = z = depth = 0  # depth: the deepest machine of the current row
    for machine_id, box in boxes:
        if x > 0 and x + box.sx > width:
            x, z, depth = 0, z + depth + _AISLE, 0
        if z + box.sz > region.sz:
            continue
        slots[machine_id] = (CellCoord(x=x, y=0, z=z),)
        x += box.sx + _CHANNEL
        depth = max(depth, box.sz)
    return slots


def _shelf_width(region: CellBox, boxes: Sequence[CellBox]) -> int:
    """How wide a row of shelves runs: the side of a square holding every box with its channel
    and aisle, at least the widest box, and at most the region."""
    spaced = sum((b.sx + _CHANNEL) * (b.sz + _AISLE) for b in boxes)
    widest = max(b.sx for b in boxes)
    return min(max(widest, math.ceil(math.sqrt(spaced))), region.sx)


#: How many connections make a single block busy enough to lift off the floor in the spaced seed
#: (module docstring): it has four faces there, so four connections leave it none to spare.
_LIFT_CONNECTIONS = 4


def _busy_blocks(problem: InputIR) -> frozenset[str]:
    """The single blocks the spaced seed lifts: at least ``_LIFT_CONNECTIONS`` connections, a port
    that may use the down face at the block's first orientation (a pinned one may not), and a front
    that stays inside the build (one facing outside sits on the boundary wherever it can)."""
    counts = connection_counts(problem.nets, problem.me_toggles)
    return frozenset(
        m.id
        for m in problem.machines
        if m.footprint.volume == 1
        and not m.fronts_outside
        and counts.get(m.id, 0) >= _LIFT_CONNECTIONS
        and any(
            Facing.DOWN in m.allowed_faces(p.id, m.orientation_options[0]) for p in m.faces.ports
        )
    )


def _lifted(
    machine: Machine, origin: CellCoord, orientation: Facing, region: CellBox, occupied: set[Cell]
) -> CellCoord:
    """``origin`` raised one cell off the floor if the cell above is free, holding the floor cell
    empty in ``occupied`` so no later machine takes it; else ``origin`` as it was."""
    above = CellCoord(x=origin.x, y=origin.y + 1, z=origin.z)
    if origin.y != 0 or not _fits(machine, above, orientation, region, occupied):
        return origin
    occupied.add((origin.x, origin.y, origin.z))
    return above


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
    machine: Machine, region: CellBox, occupied: set[Cell], first: Sequence[CellCoord] = ()
) -> tuple[CellCoord, Facing] | None:
    """The first valid (origin, orientation) for ``machine``, or ``None`` if none exists.

    A normal machine takes the first free origin with its first legal orientation. One whose front
    faces outside the build (``Machine.fronts_outside``: a power source's reserved external-feed
    face, a Crop Manager's field) must also put that front flush on the region boundary, so it
    takes the first free origin at which *some* legal orientation does that.
    ``first`` are origins to try before the plain scan (:func:`_scan_origins`).
    """
    if not machine.fronts_outside:
        orientation = machine.orientation_options[0]
        origin = _first_fit(machine, region, occupied, orientation, first)
        return None if origin is None else (origin, orientation)
    # Origin-major, exactly as before. Whether an origin is free now depends on the orientation
    # (a turned non-cubic box covers different cells), so the fit test moves inside the orientation
    # loop rather than filtering origins ahead of it. For a cubic machine - every power source
    # today - the two orders pick the same slot.
    for origin in _scan_origins(region, first):
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
    first: Sequence[CellCoord] = (),
) -> CellCoord | None:
    """The first in-bounds, non-overlapping origin for ``machine`` at ``orientation``."""
    return next(_free_origins(machine, region, occupied, orientation, first), None)


def _scan_origins(region: CellBox, first: Sequence[CellCoord] = ()) -> Iterator[CellCoord]:
    """Every origin in the region, in first-fit scan order, after the ``first`` ones offered.

    Floor layer first (``y`` outer), then rows (``z``), then columns (``x``), so layouts fill the
    ground before stacking - the buildable-compact bias, crudely. The offered origins come ahead of
    that scan, which then skips them, so a machine its offers cannot take still finds any free slot.
    """
    yield from first
    offered = {(c.x, c.y, c.z) for c in first}
    for y in range(region.sy):
        for z in range(region.sz):
            for x in range(region.sx):
                if (x, y, z) not in offered:
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
    first: Sequence[CellCoord] = (),
) -> Iterator[CellCoord]:
    """Every in-bounds, non-overlapping origin for ``machine`` at ``orientation``, in scan order."""
    for origin in _scan_origins(region, first):
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
