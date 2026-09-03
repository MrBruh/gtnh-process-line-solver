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
After the pipes are laid, each freely-placeable source is offered the free cells around the sinks
it feeds; every candidate is **really routed** with :func:`router.route_power` and ranked by the
same key the feedback loop ranks whole attempts on (``_structure.structure_quality``). A candidate
is adopted only if it is strictly better on that key, so the repair can never cost the loop a
layout it would have preferred - it either improves the structure or leaves it exactly alone.

    placements -> route items/fluids -> for each source: try the free cells near its sinks,
                                                         route_power each, keep the best
               -> the winning power routing, handed straight back to the caller

Sources are walked in problem order and candidate poses in sorted order, so the pass is
deterministic for a given placement.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from gtnh_solver.ir import CellCoord, Commodity, Facing, InputIR, Machine, Placement, Route
from gtnh_solver.ir.geometry import (
    FACE_OFFSETS,
    Cell,
    box_in_region,
    front_on_boundary,
    occupied_cells,
)
from gtnh_solver.ir.nets import net_sources_sinks, port_direction_map
from gtnh_solver.placement import Objective
from gtnh_solver.router import PowerRouteResult, route_power

from ._structure import structure_quality

#: A candidate's rank: unroutable power nets first (a relocation that rescues one always wins),
#: then the loop's own (compactness, cable cells, compactness) quality key.
_Score = tuple[int, tuple[int, int, int]]


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
    sinks_of = _power_sinks_by_source(problem)
    reserved = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    occupied = {
        c
        for p in current
        for c in occupied_cells(p.cell, machines[p.machine_id].footprint, p.orientation)
    }
    # Machine order, not placement order: an accepted LNS move reorders the placement list, and a
    # sweep that inherited that order would not be deterministic for a given seed.
    for machine in problem.machines:
        sink_ids = sinks_of.get(machine.id)
        if not sink_ids:
            continue  # not a power source, or its only net is externally pinned
        index = next(i for i, p in enumerate(current) if p.machine_id == machine.id)
        here = current[index]
        body = set(occupied_cells(here.cell, machine.footprint, here.orientation))
        # Ground the source may not stand on, once its own body is lifted out of the way. The
        # pipe cells are in there because a machine body over a route is a validator violation.
        blocked = (occupied - body) | reserved | item_cells
        best_score = _score(problem, current, item_routes, best_result, objective)
        best_pose, moved = here, False
        for origin, facing in _candidate_poses(machine, sink_ids, current, machines, problem):
            if (origin, facing) == (here.cell, here.orientation):
                continue
            if set(occupied_cells(origin, machine.footprint, facing)) & blocked:
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


def _power_sinks_by_source(problem: InputIR) -> dict[str, set[str]]:
    """Each power source's consumer machine ids, over every power net it feeds.

    A net carrying an external **pinned** I/O cell is skipped: its route is constrained to ground
    the power router does not itself model (the pin is validator-enforced), so relocating its
    source could invalidate a layout this pass is not equipped to re-check. A source left with no
    consumers is absent from the result and never relocated.
    """
    pinned = {pin.net_id for pin in problem.pinned}
    port_dir = port_direction_map(problem)
    sinks: dict[str, set[str]] = {}
    for net in problem.nets:
        if net.commodity is not Commodity.POWER or net.id in pinned:
            continue
        sources, net_sinks = net_sources_sinks(net, port_dir)
        sink_ids = {e.machine_id for e in net_sinks}
        for endpoint in sources:
            sinks.setdefault(endpoint.machine_id, set()).update(sink_ids)
    return {sid: ids - {sid} for sid, ids in sinks.items() if ids - {sid}}


def _candidate_poses(
    machine: Machine,
    sink_ids: set[str],
    placements: Sequence[Placement],
    machines: Mapping[str, Machine],
    problem: InputIR,
) -> list[tuple[CellCoord, Facing]]:
    """Every legal pose for ``machine`` face-adjacent to a body cell of one of its sinks.

    The candidate ground is the shell one step out from the sinks' bodies - where a source can
    dock straight onto its consumer, which is exactly what the 3-cable sand layout is. A pose is
    kept only if it is one of the machine's **declared orientations** (anything else is a
    BAD_ORIENTATION violation), lies in-region, and keeps its **front (feed) face flush on a
    region wall** - the same hard constraint the constructive seed and the annealer maintain and
    the validator enforces. Those rules are what keep the set small (tens of poses, not
    thousands), so really routing every one of them stays cheap. Sorted, for a deterministic sweep.
    """
    region = problem.bounding_region
    shell: set[Cell] = set()
    for p in placements:
        if p.machine_id not in sink_ids:
            continue
        sink = machines[p.machine_id]
        for cell in occupied_cells(p.cell, sink.footprint, p.orientation):
            for dx, dy, dz in FACE_OFFSETS:
                shell.add((cell[0] + dx, cell[1] + dy, cell[2] + dz))
    poses: list[tuple[CellCoord, Facing]] = []
    for x, y, z in sorted(shell):
        origin = CellCoord(x=x, y=y, z=z)
        for facing in machine.orientation_options:
            if box_in_region(origin, machine.footprint, facing, region) and front_on_boundary(
                origin, machine.footprint, facing, region
            ):
                poses.append((origin, facing))
    return poses
