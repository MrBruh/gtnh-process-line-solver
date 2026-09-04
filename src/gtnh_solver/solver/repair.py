"""solver.repair - relocate each power source onto the cable it actually costs.

A power source's position exists purely to serve a cable, and it is the one machine the annealer
has no gradient on: a 1x1x1 source anywhere inside the build's bounding box is cost-neutral to
move, so the only term that touches it is compactness (#123).

The obvious fix - give power nets a center-distance pull in ``placement.search`` - was tried and
measured, twice. It loses, because a center-distance proxy cannot see dock faces or shared cable
taps: the shipped sand line puts its source *on top* of the machine row so the hammers tap its dock
cell through their top faces (3 cable cells), and a proxy scores a nearer-but-worse position
higher. Re-measured 2026-09-03 at four weights down to 0.1, sand's cable went UP at every one
(3 -> 4..6 cells) - the finding behind PR #62 still binds.

So this judges cable where it IS knowable, on a routed layout, exactly as that decision says to.
After the pipes are laid, each source is offered the poses nearest the load it serves, every one is
**really routed** with :func:`router.route_power`, and the best is ranked by the same key the
feedback loop ranks whole attempts on (``_structure.structure_quality``). A candidate is adopted
only if it is strictly better on that key, so the repair can never cost the loop a layout it would
have preferred - it either improves the structure or leaves it exactly alone.

    placements -> route items/fluids -> for each source: aim at its first trunk branch (or its
                                                         only connection), take the nearest legal
                                                         wall poses, route_power each, keep the
                                                         best
               -> the winning power routing, handed straight back to the caller

**Aim at the load, then go to the wall** - not the other way around. A source's feed face has to
stay flush on a region wall, so its legal ground is the wall planes, which can be several cells
from the load it serves. Choosing candidates by adjacency to a sink therefore intersects two
constraints that often miss each other entirely: on nitrobenzene the MV source's only consumer
spans z3..5 and the nearest wall a horizontal feed face can use is z=0, so an adjacency shell of
radius 1 offered it **no pose at all** and it sat 10 cells off along the wall from its load,
dragging a 14-cell trunk. Aiming at the load and projecting to the wall always yields candidates,
because the nearest wall pose to a point is always defined.

The aim only ever *shortlists*; :func:`route_power` still decides. That is what keeps #62's finding
from creeping back in: a proxy that ranks candidates can miss a good one, but it cannot promote a
bad one past a real routing, so the worst case is a missed win rather than a worse layout.
:data:`_N_CANDIDATES` of 20 was measured, not guessed - it reaches the same cable count as scoring
every legal pose on both shipped examples and all three objectives, for about 60 routings instead
of 5200.

Sources are walked in problem order and candidate poses in a fully ordered ranking, so the pass is
deterministic for a given placement.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from gtnh_solver.ir import CellBox, CellCoord, Commodity, Facing, InputIR, Machine, Placement, Route
from gtnh_solver.ir.geometry import Cell, occupied_cells, rotated_footprint
from gtnh_solver.ir.nets import net_sources_sinks, port_direction_map
from gtnh_solver.placement import Objective
from gtnh_solver.router import PowerRouteResult, route_power

from ._structure import structure_quality

#: A candidate's rank: unroutable power nets first (a relocation that rescues one always wins),
#: then the loop's own (compactness, cable cells, compactness) quality key.
_Score = tuple[int, tuple[int, int, int]]

#: How many of the nearest legal wall poses to really route per source. Measured, not guessed: 20
#: matches an exhaustive scan of every legal pose on sand and nitrobenzene across all three
#: objectives, and 100 buys nothing more - the ranking puts the winner near the front, and the tail
#: only covers the case where the closest poses are blocked or displace the trunk.
_N_CANDIDATES = 20


@dataclass(frozen=True)
class _SourceLoad:
    """What one power source has to reach: the nets it feeds and the machines on them."""

    net_ids: frozenset[str]
    sink_ids: frozenset[str]


def repair_power_sources(
    problem: InputIR,
    placements: Sequence[Placement],
    *,
    item_routes: Sequence[Route],
    claimed_cells: Mapping[str, Collection[Cell]],
    objective: Objective,
) -> tuple[list[Placement], PowerRouteResult]:
    """Route power, relocating each power source to the best cell the routed cable can find.

    Returns the (possibly improved) placements and the power routing that goes with them - the
    winning candidate's own result, not a re-route, so the caller assembles exactly the layout
    that was scored. ``item_routes`` are the pipes already laid: their cells are obstacles for the
    cable and forbidden ground for a relocated source, and they take part in the compactness
    ranking. ``claimed_cells`` is the per-machine casing budget those pipes' hatches already spent
    (:func:`router.claims_by_machine`), passed through to the power router unchanged.
    """
    item_cells = {cell for r in item_routes for cell in r.cells()}

    def lay(candidate: Sequence[Placement]) -> PowerRouteResult:
        return route_power(
            problem, candidate, extra_obstacles=item_cells, claimed_cells=claimed_cells
        )

    current = list(placements)
    best_result = lay(current)
    if problem.me_toggles.toggled(Commodity.POWER):
        return current, best_result  # power rides the ME network; there is no cable to shorten
    if best_result.failed_nets or best_result.infeasibility is not None:
        # This placement cannot carry its power at all, so the caller is about to hand it to the
        # feedback loop as a *diagnosis*: these nets failed, penalize them and re-place. Shuffling
        # sources first would change which nets fail and hand the loop a different story each
        # time - and the loop stops early when a failed-net set repeats, so the extra churn cost
        # nitrobenzene/balanced its valid layout entirely (it broke off after three attempts
        # instead of eight). Improve layouts that work; never edit the evidence from one that
        # does not.
        return current, best_result

    machines = {m.id: m for m in problem.machines}
    loads = _power_loads_by_source(problem)
    reserved = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    occupied = {
        c
        for p in current
        for c in occupied_cells(p.cell, machines[p.machine_id].footprint, p.orientation)
    }
    # Machine order, not placement order: an accepted LNS move reorders the placement list, and a
    # sweep that inherited that order would not be deterministic for a given seed.
    for machine in problem.machines:
        load = loads.get(machine.id)
        if load is None:
            continue  # not a power source, or its only net is externally pinned
        index = next(i for i, p in enumerate(current) if p.machine_id == machine.id)
        here = current[index]
        body = set(occupied_cells(here.cell, machine.footprint, here.orientation))
        # Ground the source may not stand on, once its own body is lifted out of the way. The
        # pipe cells are in there because a machine body over a route is a validator violation.
        blocked = (occupied - body) | reserved | item_cells
        best_score = _score(problem, current, item_routes, best_result, objective)
        best_pose, moved = here, False
        poses = _candidate_poses(
            machine, load, here, current, machines, problem, best_result.routes, blocked
        )
        for origin, facing in poses:
            if (origin, facing) == (here.cell, here.orientation):
                continue
            candidate = list(current)
            candidate[index] = Placement(machine_id=machine.id, cell=origin, orientation=facing)
            result = lay(candidate)
            score = _score(problem, candidate, item_routes, result, objective)
            if score < best_score:
                best_score, best_pose, best_result, moved = score, candidate[index], result, True
        if moved:
            current[index] = best_pose
            occupied = (occupied - body) | set(
                occupied_cells(best_pose.cell, machine.footprint, best_pose.orientation)
            )
    return current, best_result


def _score(
    problem: InputIR,
    placements: Sequence[Placement],
    item_routes: Sequence[Route],
    power: PowerRouteResult,
    objective: Objective,
) -> _Score:
    """Rank a candidate: fewest unroutable power nets first, then the loop's quality key over the
    whole structure (machines + pipes + this candidate's cable). A stall that names no net still
    counts as one failure, so a candidate that routes cleanly outranks it."""
    unroutable = len(power.failed_nets)
    if not unroutable and power.infeasibility is not None:
        unroutable = 1
    routes = [*item_routes, *power.routes]
    return unroutable, structure_quality(problem, placements, routes, objective)


def _power_loads_by_source(problem: InputIR) -> dict[str, _SourceLoad]:
    """Each power source's nets and consumer machine ids, over every power net it feeds.

    A net carrying an external **pinned** I/O cell is skipped: its route is constrained to ground
    the power router does not itself model (the pin is validator-enforced), so relocating its
    source could invalidate a layout this pass is not equipped to re-check. A source left with no
    consumers is absent from the result and never relocated.
    """
    pinned = {pin.net_id for pin in problem.pinned}
    port_dir = port_direction_map(problem)
    nets: dict[str, set[str]] = defaultdict(set)
    sinks: dict[str, set[str]] = defaultdict(set)
    for net in problem.nets:
        if net.commodity is not Commodity.POWER or net.id in pinned:
            continue
        sources, net_sinks = net_sources_sinks(net, port_dir)
        sink_ids = {e.machine_id for e in net_sinks}
        for endpoint in sources:
            nets[endpoint.machine_id].add(net.id)
            sinks[endpoint.machine_id].update(sink_ids)
    return {
        sid: _SourceLoad(net_ids=frozenset(nets[sid]), sink_ids=frozenset(ids - {sid}))
        for sid, ids in sinks.items()
        if ids - {sid}
    }


def _candidate_poses(
    machine: Machine,
    load: _SourceLoad,
    here: Placement,
    placements: Sequence[Placement],
    machines: Mapping[str, Machine],
    problem: InputIR,
    power_routes: Sequence[Route],
    blocked: Collection[Cell],
    limit: int = _N_CANDIDATES,
) -> list[tuple[CellCoord, Facing]]:
    """The ``limit`` legal poses nearest the load this source serves, nearest first.

    Aim first, then project to the wall (module docstring): the target is the first branch of the
    source's own trunk - where the cable stops being one shared run and starts serving consumers
    separately - or, for a net that never branches, the middle of the machines it feeds. Poses are
    the free wall planes, ranked by Manhattan distance to that target and broken by coordinate so
    the ranking is total and the sweep deterministic.
    """
    target = _target_cell(load, here, placements, machines, power_routes)
    taken = set(blocked)  # hoisted: the wall planes run to thousands of poses
    poses = [
        (origin, facing)
        for origin, facing in _wall_poses(machine, problem.bounding_region)
        if taken.isdisjoint(occupied_cells(origin, machine.footprint, facing))
    ]
    poses.sort(
        key=lambda p: (
            abs(p[0].x - target[0]) + abs(p[0].y - target[1]) + abs(p[0].z - target[2]),
            p[0].x,
            p[0].y,
            p[0].z,
            p[1].value,
        )
    )
    return poses[:limit]


def _target_cell(
    load: _SourceLoad,
    here: Placement,
    placements: Sequence[Placement],
    machines: Mapping[str, Machine],
    power_routes: Sequence[Route],
) -> Cell:
    """The cell this source wants to sit near: its trunk's first branch, else its load's middle.

    A shared-amperage trunk is a tree, so the branch nearest the source is where its one shared run
    ends - put the source there and every consumer's leg is as short as the geometry allows. A net
    that never branches (a single consumer, or a straight run) has no such cell, so the target
    falls back to the centre of the machines it feeds, which for one consumer is that consumer.
    The result is only an aim; the caller really routes what it shortlists.
    """
    degree: dict[Cell, int] = defaultdict(int)
    for route in power_routes:
        if route.net_id not in load.net_ids:
            continue
        for seg in route.segments:
            start = (seg.start.x, seg.start.y, seg.start.z)
            end = (seg.end.x, seg.end.y, seg.end.z)
            if start != end:
                degree[start] += 1
                degree[end] += 1
    branches = [cell for cell, deg in degree.items() if deg >= 3]
    if branches:
        origin = (here.cell.x, here.cell.y, here.cell.z)
        return min(
            branches,
            key=lambda c: (
                abs(c[0] - origin[0]) + abs(c[1] - origin[1]) + abs(c[2] - origin[2]),
                c,
            ),
        )
    cells = [
        cell
        for p in placements
        if p.machine_id in load.sink_ids
        for cell in occupied_cells(p.cell, machines[p.machine_id].footprint, p.orientation)
    ]
    if not cells:  # pragma: no cover - a source with no placed consumer never reaches here
        return (here.cell.x, here.cell.y, here.cell.z)
    n = len(cells)
    return (
        sum(c[0] for c in cells) // n,
        sum(c[1] for c in cells) // n,
        sum(c[2] for c in cells) // n,
    )


def _wall_poses(machine: Machine, region: CellBox) -> list[tuple[CellCoord, Facing]]:
    """Every in-region pose of ``machine`` whose front (feed) face is flush on a region wall.

    Enumerated per declared orientation over the one wall plane that orientation can sit on, rather
    than by scanning the region and testing each cell: being flush pins one coordinate exactly, so
    this walks the wall's *area* (about 1800 poses on nitrobenzene) instead of its volume (85000
    candidates, all but 1800 rejected). Only declared orientations are offered, since any other is
    a BAD_ORIENTATION violation. The vertical cases are carried for completeness against
    :func:`ir.geometry.front_on_boundary`, which the IR keeps unreachable by rejecting a machine
    whose front is not horizontal.
    """
    poses: list[tuple[CellCoord, Facing]] = []
    for facing in machine.orientation_options:
        box = rotated_footprint(machine.footprint, facing)
        # The free span along each axis, then the flush rule pins whichever axis the face points
        # down - exactly the predicate ir.geometry.front_on_boundary checks.
        xs = range(region.sx - box.sx + 1)
        ys = range(region.sy - box.sy + 1)
        zs = range(region.sz - box.sz + 1)
        if facing is Facing.NORTH:
            zs = range(1)
        elif facing is Facing.SOUTH:
            zs = range(region.sz - box.sz, region.sz - box.sz + 1)
        elif facing is Facing.WEST:
            xs = range(1)
        elif facing is Facing.EAST:
            xs = range(region.sx - box.sx, region.sx - box.sx + 1)
        elif facing is Facing.DOWN:  # pragma: no cover - the IR rejects a vertical front
            ys = range(1)
        else:  # pragma: no cover - UP, likewise
            ys = range(region.sy - box.sy, region.sy - box.sy + 1)
        poses.extend((CellCoord(x=x, y=y, z=z), facing) for x in xs for y in ys for z in zs)
    return poses
