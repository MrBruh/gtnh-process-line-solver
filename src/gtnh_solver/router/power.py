"""router.power - the shared-amperage power router.

Power is **not** a disjoint per-pipe flow: a source feeds a cable trunk that machines tap, and
the amperage **sums** along the shared segments toward the source (docs/DOMAIN.md). This router
turns each per-tier power net (one source endpoint + that tier's machine endpoints, synthesized
by the adapter) into a trunk and sizes every segment to the amperage flowing through it.

ASCII (one tier, a path-trunk; thickness sized to the summed downstream amperage)::

    source ===4x=== m0(1A) ==2x== m1(1A) ==1x== m2(1A)
           |<- 3A ->|<-- 2A --->|<-- 1A -->|

**Cable loss:** GT cables lose voltage over distance, so a machine ``d`` blocks down the trunk
receives ``tier_voltage - d`` volts (docs/DOMAIN.md). The source stays at its tier and the cable
is thickened to compensate: a machine's amperage is sized at its *delivered* voltage
(``ceil(eut / (tier_voltage - d))``), so farther machines cost more amps for the same ``eut``. A
run so long that the delivered voltage reaches 0 cannot be powered at this tier and is rejected.

Each machine docks **route-aware**: rather than committing a terminal on a fixed face, the router
considers every usable (non-front) face and, via multi-goal A*, docks on whichever one yields the
shortest cable to the trunk (``_grid.dock_candidates`` + ``astar_multi``). A cable connects to any
face but the front, so pinning one face up front just made the trunk snake around the machine.

Crude on purpose (correctness-first, the handoff sequencing): **one source per tier**, the trunk
is a path through the machines in endpoint order (a multi-goal A* leg per machine over the shared
``_grid``, each leg avoiding the cells already laid so the legs never overlap and the trunk stays a
tree), and a leg needing **> 16x** (or a run too long to keep any voltage) is rejected (Phase 2
adds multi-source / parallel-run / voltage-upgrade optimization). The validator independently
re-derives each machine's distance, delivered voltage, and the per-segment amperage, so a sizing
bug here is caught, not certified.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from itertools import pairwise

from gtnh_solver.dataset import UnknownTierError, UnpowerableError, amperage
from gtnh_solver.ir import (
    CellBox,
    Commodity,
    Infeasibility,
    InputIR,
    IODirection,
    Machine,
    MachineFaceRef,
    Placement,
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.ir.geometry import Cell

from ._grid import astar_multi, coord, dock_candidates, manhattan, obstacle_cells

#: GT cable thicknesses, smallest first (16x is the hard cap - docs/DOMAIN.md).
_THICKNESSES = (1, 2, 4, 8, 16)
_MAX_THICKNESS = _THICKNESSES[-1]


@dataclass(frozen=True)
class PowerRouteResult:
    """Power router output: all power routes, or a partial set plus why it stalled.

    ``failed_nets`` names the power net that stalled (empty when ``ok``), so the solver's
    place<->route feedback loop can penalize it and re-place (helps a dock/path failure; an
    amperage/tier failure is not placement-fixable, but the loop's cycle detection stops quickly).
    """

    routes: tuple[Route, ...] = ()
    infeasibility: Infeasibility | None = None
    failed_nets: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True iff every (non-ME) power net was routed and amperage-feasible."""
        return self.infeasibility is None


def route_power(
    problem: InputIR,
    placements: Sequence[Placement],
    *,
    extra_obstacles: Collection[Cell] = (),
) -> PowerRouteResult:
    """Route each per-tier power net of ``problem`` as a shared-amperage trunk over ``placements``.

    ``extra_obstacles`` are cells already taken by other routes (the item/fluid pipes the solver
    laid first), so power cables never share a cell with them - the crude single-channel capacity.
    Each tier's trunk is likewise added to the obstacle set before the next tier routes, so two
    power trunks never collide either.
    """
    machines = {m.id: m for m in problem.machines}
    placement_by_machine: dict[str, Placement] = {}
    for placement in placements:
        placement_by_machine.setdefault(placement.machine_id, placement)  # one placement/machine
    region = problem.bounding_region
    port_dir = {(m.id, p.id): p.direction for m in problem.machines for p in m.faces.ports}

    if problem.me_toggles.toggled(Commodity.POWER):
        return PowerRouteResult()  # power is on the ME network; nothing to route

    obstacles = obstacle_cells(problem, placements, machines) | set(extra_obstacles)
    docked: set[Cell] = set()
    routes: list[Route] = []
    for net in problem.nets:
        if net.commodity is not Commodity.POWER:
            continue
        sources = [e for e in net.endpoints if port_dir.get(_key(e)) is IODirection.OUTPUT]
        sinks = [e for e in net.endpoints if port_dir.get(_key(e)) is IODirection.INPUT]
        if len(sources) != 1 or not sinks:
            return PowerRouteResult(tuple(routes), _malformed(net.id), (net.id,))

        built = _route_trunk(
            net.id,
            sources[0],
            sinks,
            placement_by_machine,
            machines,
            obstacles,
            docked,
            region,
        )
        if isinstance(built, Infeasibility):
            return PowerRouteResult(tuple(routes), built, (net.id,))
        routes.append(built)
        # Capacity: this trunk now owns these cells, so the next tier's trunk routes around it.
        for seg in built.segments:
            obstacles.add((seg.start.x, seg.start.y, seg.start.z))
            obstacles.add((seg.end.x, seg.end.y, seg.end.z))
    return PowerRouteResult(tuple(routes))


def _key(endpoint: MachineFaceRef) -> tuple[str, str]:
    return (endpoint.machine_id, endpoint.port_id)


def _route_trunk(
    net_id: str,
    source: MachineFaceRef,
    sinks: Sequence[MachineFaceRef],
    placement_by_machine: dict[str, Placement],
    machines: dict[str, Machine],
    obstacles: set[Cell],
    docked: set[Cell],
    region: CellBox,
) -> Route | Infeasibility:
    """Dock every machine route-aware and chain source->m0->m1->... into a shared-amperage trunk.

    Each machine docks on whichever usable (non-front) face gives the shortest cable: the source
    toward its first sink, then each sink via a multi-goal A* leg from the running trunk end to any
    of that sink's free dock cells (``dock_candidates`` + ``astar_multi``). The cell the leg reaches
    is the terminal, so routing - not a fixed face order - chooses the face. Leg lengths accumulate
    into each machine's cable-block **distance** from the source; amperage is then sized at the
    machine's *delivered* voltage, so cable loss thickens the run (``_size_trunk``).

    Each laid leg's cells are blocked for the legs that follow, so two legs never share a cell: the
    trunk stays a simple non-self-crossing path (a tree the validator can root at the source). A
    machine with no free non-front dock face is a ``face_reachability`` infeasibility; one no leg
    can reach is ``routing``; an unpowerable / over-16x run is rejected on sizing - never certified.
    """
    endpoints = [source, *sinks]
    candidates: list[list[Terminal]] = []
    for ep in endpoints:
        placement = placement_by_machine.get(ep.machine_id)
        machine = machines.get(ep.machine_id)
        if placement is None or machine is None:
            return _no_dock(net_id)
        cand = dock_candidates(ep.port_id, placement, machine, obstacles, docked, region)
        if not cand:
            return _no_dock(net_id)
        candidates.append(cand)

    # Dock the source on the face nearest its first sink, then reserve that cell so no sink reuses it.
    source_terminal = _nearest(candidates[0], candidates[1])
    docked.add(_cell(source_terminal))

    terminals: list[Terminal] = [source_terminal]
    legs: list[list[Cell]] = []
    distances: list[int] = []  # distances[i] = cable-blocks from the source to sink m_i
    blocked = set(obstacles)  # grows with each laid leg to keep the trunk a non-overlapping path
    distance = 0
    prev = _cell(source_terminal)
    for cand in candidates[1:]:
        goals = {_cell(t) for t in cand} - docked - blocked
        if not goals:
            return _no_dock(net_id)  # every usable face is taken by the trunk already laid
        path = astar_multi([prev], goals, blocked, region)
        if path is None:
            return Infeasibility(
                constraint="routing",
                detail=f"power net {net_id!r}: no free cell path to a dock face of "
                f"{cand[0].machine_id!r}",
                suggested_relaxation="enlarge the bounding region or reduce obstacles",
            )
        end = path[-1]
        terminals.append(next(t for t in cand if _cell(t) == end))
        docked.add(end)
        distance += len(path) - 1  # cable-block hops added by this leg (endpoints shared, no +1)
        distances.append(distance)
        blocked.update(path)  # later legs must avoid these cells so the graph stays a tree
        legs.append(path)
        prev = end

    loads = [(machines[e.machine_id].eut, machines[e.machine_id].voltage_tier) for e in sinks]
    sized = _size_trunk(net_id, legs, distances, loads)
    if isinstance(sized, Infeasibility):
        return sized
    segments, thickness = sized
    return Route(
        net_id=net_id,
        commodity=Commodity.POWER,
        terminals=tuple(terminals),
        segments=segments,
        thickness_per_segment=thickness,
    )


def _size_trunk(
    net_id: str,
    legs: Sequence[Sequence[Cell]],
    distances: Sequence[int],
    loads: Sequence[tuple[float, str]],
) -> tuple[list[Segment], list[int]] | Infeasibility:
    """Size each leg to the summed downstream amperage at the delivered (loss-reduced) voltage.

    ``loads[i]`` / ``distances[i]`` are sink ``m_i``'s ``(eut, tier)`` and its cable-block distance
    from the source. Amperage is sized at each machine's delivered voltage
    (``ceil(eut / (tier_voltage - distance))``), so cable loss thickens the run instead of
    under-powering the far machines. Leg ``i`` carries every machine from ``m_i`` onward, so its
    load is ``sum(amps[i:])`` - the suffix sum that defines a shared-amperage trunk. A run whose
    delivered voltage reaches 0 (:class:`UnpowerableError`) or whose summed load exceeds 16x is
    rejected, not silently certified.
    """
    amps: list[int] = []
    for (eut, tier), dist in zip(loads, distances, strict=True):
        try:
            amps.append(amperage(eut, tier, distance=dist))
        except UnknownTierError:
            return Infeasibility(
                constraint="voltage_tier",
                detail=f"power net {net_id!r} serves an unknown voltage tier {tier!r}",
                suggested_relaxation="add the tier to dataset.VOLTAGE_BY_TIER, or fix the export tier",
            )
        except UnpowerableError as exc:
            return Infeasibility(
                constraint="voltage_drop",
                detail=f"power net {net_id!r}: {exc}",
                suggested_relaxation="place the machine nearer the source, split the net, or use a "
                "higher voltage tier - Phase 2 multi-source optimization",
            )

    segments: list[Segment] = []
    thickness: list[int] = []
    for i, path in enumerate(legs):
        leg_load = sum(amps[i:])  # machines m_i.. are downstream of this leg
        if leg_load > _MAX_THICKNESS:
            return Infeasibility(
                constraint="amperage",
                detail=f"power net {net_id!r}: a cable segment must carry {leg_load} amps, over the "
                "16x cable cap",
                suggested_relaxation="split into parallel runs or use a higher voltage tier "
                "(more power per amp) - Phase 2 multi-source optimization",
            )
        thick = _cable_thickness(leg_load)
        for c0, c1 in pairwise(path):
            segments.append(Segment(start=coord(c0), end=coord(c1), channel=0))
            thickness.append(thick)
    return segments, thickness


def _nearest(candidates: Sequence[Terminal], targets: Sequence[Terminal]) -> Terminal:
    """The candidate terminal whose cell is closest (Manhattan) to any target cell.

    Docks the source facing its first sink; ties keep the earlier candidate (``FACE_ORDER``).
    """
    target_cells = [_cell(t) for t in targets]
    return min(candidates, key=lambda t: min(manhattan(_cell(t), tc) for tc in target_cells))


def _cell(terminal: Terminal) -> Cell:
    return (terminal.cell.x, terminal.cell.y, terminal.cell.z)


def _cable_thickness(load: int) -> int:
    """Smallest cable thickness (1/2/4/8/16) that carries ``load`` amps (>=1 even for 0)."""
    for t in _THICKNESSES:
        if load <= t:
            return t
    return _MAX_THICKNESS  # the caller already rejected load > 16


def _malformed(net_id: str) -> Infeasibility:
    return Infeasibility(
        constraint="power_net",
        detail=f"power net {net_id!r} is not one source + >=1 sink (cannot form a trunk)",
        suggested_relaxation="check the power synthesis: each tier net needs exactly one source",
    )


def _no_dock(net_id: str) -> Infeasibility:
    return Infeasibility(
        constraint="face_reachability",
        detail=f"power net {net_id!r} could not dock a terminal (no free non-front face cell)",
        suggested_relaxation="free up adjacent cells, or leave routing gaps around machines",
    )
