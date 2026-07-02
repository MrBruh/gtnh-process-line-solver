"""router.power - the shared-amperage power router.

Power is **not** a disjoint per-pipe flow: a source feeds a cable trunk that machines tap, and
the amperage **sums** along the shared segments toward the source (docs/DOMAIN.md). This router
turns each per-tier power net (one source endpoint + that tier's machine endpoints, synthesized
by the adapter) into a trunk and sizes every segment to the amperage flowing through it.

ASCII (one tier; the trunk is a TREE rooted at the source's dock cell)::

                     m2 (taps [C]: terminal on the trunk cell, no new cable)
                      |
    source ===4x=== [C] ==2x== m1(1.5A)
                      |
                     2x
                      |
                    m0(1.5A)

    Each segment carries the summed load of the sink terminals on its far-from-root side, rounded
    up to whole amps per segment: both branch legs carry only their own sink's 1.5A (a 2x cable),
    while the root carries 3A summed BEFORE rounding (the old path-trunk's suffix sum would have
    overcharged a branch); m2 draws straight from the shared cell [C], loading no segment.

**Cable loss:** GT cables lose voltage over distance, so a machine whose terminal sits ``d``
cable-blocks from the source (its cell's depth in the tree) receives ``tier_voltage - d`` volts
(docs/DOMAIN.md). The source stays at its tier and the cable is thickened to compensate: a
machine's load is sized at its *delivered* voltage (``eut / (tier_voltage - d)``, fractional -
machines buffer packets and average below a whole amp, see ``dataset.amp_load``), so farther
machines load the net more for the same ``eut``; only each segment's summed load rounds up to
whole amps. A run so long that the delivered voltage reaches 0 cannot be powered at this tier and
is rejected.

Each machine docks **route-aware**: rather than committing a terminal on a fixed face, the router
considers every usable (non-front) face and, via multi-goal A*, docks on whichever one yields the
shortest cable to the trunk (``_grid.dock_candidates`` + ``astar_multi``). A cable connects to any
face but the front, so pinning one face up front just made the trunk snake around the machine.

**The trunk is a tree with shared taps.** In GT one cable block feeds every machine face wired to
it, so terminals of one net may share cells: a sink with a dock candidate that is already a trunk
cell TAPS it (its terminal lands on that cell, no new cable), and any other sink extends the tree
with a multi-goal A* leg from *all* trunk cells laid so far. Laid cells stay blocked for later
legs (a leg may attach at a trunk cell but never cross one), so the trunk is always a single tree
the validator can root at the source.

Crude on purpose (correctness-first, the handoff sequencing): **one source per tier**, sinks are
taken in net-endpoint order (no sink-order optimization), and a segment needing **> 16x** (or a
run too long to keep any voltage) is rejected (Phase 2 adds multi-source / parallel-run /
voltage-upgrade optimization). The validator independently re-derives each machine's distance,
delivered voltage, and the per-segment amperage, so a sizing bug here is caught, not certified.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise

from gtnh_solver.dataset import (
    CABLE_THICKNESSES,
    MAX_CABLE_THICKNESS,
    UnknownTierError,
    UnpowerableError,
    amp_load,
    whole_amps,
)
from gtnh_solver.ir import (
    CellBox,
    Commodity,
    Infeasibility,
    InputIR,
    Machine,
    MachineFaceRef,
    Net,
    Placement,
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.ir.geometry import Cell
from gtnh_solver.ir.nets import net_sources_sinks, placement_index, port_direction_map

from ._grid import astar_multi, coord, dock_candidates, manhattan, obstacle_cells
from .core import _rip_up_reroute


@dataclass(frozen=True)
class PowerRouteResult:
    """Power router output: all power routes, or a partial set plus why it stalled.

    ``failed_nets`` lists every power net still unrouted after the failed-first rip-up/reroute
    retry (empty when ``ok``), in problem order, so the solver's place<->route feedback loop can
    penalize them all and re-place (helps a dock/path failure; an amperage/tier failure is not
    placement-fixable, but the loop's cycle detection stops quickly). ``infeasibility`` carries the
    first one's specific reason, matching the item router's reporting shape.
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
    power trunks never collide - and since every terminal of a finished net (docked or tapped)
    sits on one of its segment cells, the same set keeps later nets from docking on this trunk.
    Terminals may share cells only *within* one net (a tap on its own trunk).

    Because that accretion is order-dependent - a trunk laid for one tier can wedge a later tier's
    trunk out of a chokepoint - the tiers are routed with **failed-first rip-up/reroute** (the same
    bounded retry the item router uses, ``core._rip_up_reroute``): route a pass, and if any net
    failed, rip every trunk up and retry with the failed nets first, until a pass is clean or the
    failed-net set repeats (a genuine infeasibility, not a tier-ordering accident). This keeps the
    solver's feedback loop from getting a false infeasibility on power that it would not get on
    pipes. When routing genuinely stalls, ALL still-failing nets are reported (#40), not just the
    first. (The buildguide half of #40 - branching-trunk rendering - is parked; not touched here.)
    """
    if problem.me_toggles.toggled(Commodity.POWER):
        return PowerRouteResult()  # power is on the ME network; nothing to route

    power_nets = [net for net in problem.nets if net.commodity is Commodity.POWER]
    routes, failures = _rip_up_reroute(
        power_nets, lambda order: _route_pass(problem, placements, order, extra_obstacles)
    )
    if not failures:
        return PowerRouteResult(routes=tuple(routes))

    # Exhausted: report the first net still failing (in problem order), with its specific reason,
    # plus every still-failing net so the solver's feedback loop can penalize them all.
    still_failing = tuple(net.id for net in power_nets if net.id in failures)
    return PowerRouteResult(
        routes=tuple(routes),
        infeasibility=failures[still_failing[0]],
        failed_nets=still_failing,
    )


def _route_pass(
    problem: InputIR,
    placements: Sequence[Placement],
    nets: Sequence[Net],
    extra_obstacles: Collection[Cell] = (),
) -> tuple[list[Route], dict[str, Infeasibility]]:
    """Route ``nets`` once in the given order as shared-amperage trunks, capacity-aware.

    Mirrors ``core._route_pass`` for power: builds the obstacle set fresh (reserved + machine
    bodies + ``extra_obstacles``), then grows each net's trunk in turn, adding a finished trunk's
    cells to the obstacles so later tiers route around it. Failures are order-dependent - a
    malformed net (not one source + >=1 sink), an undockable/unroutable trunk, or an
    amperage/tier/voltage rejection - so this skips (does not abort on) them and records why per
    net; the caller (:func:`_rip_up_reroute`) retries with a different order. A failed trunk leaves
    no trace: ``_route_trunk`` never mutates ``obstacles``, so the retry starts clean.
    """
    machines = {m.id: m for m in problem.machines}
    placement_by_machine = placement_index(placements)
    region = problem.bounding_region
    port_dir = port_direction_map(problem)

    obstacles = obstacle_cells(problem, placements, machines) | set(extra_obstacles)
    routes: list[Route] = []
    failures: dict[str, Infeasibility] = {}
    for net in nets:
        sources, sinks = net_sources_sinks(net, port_dir)
        if len(sources) != 1 or not sinks:
            failures[net.id] = _malformed(net.id)
            continue
        built = _route_trunk(
            net.id,
            sources[0],
            sinks,
            placement_by_machine,
            machines,
            obstacles,
            region,
        )
        if isinstance(built, Infeasibility):
            failures[net.id] = built
            continue
        routes.append(built)
        # Capacity: this trunk now owns these cells, so the next tier's trunk routes around it.
        obstacles.update(built.cells())
    return routes, failures


def _route_trunk(
    net_id: str,
    source: MachineFaceRef,
    sinks: Sequence[MachineFaceRef],
    placement_by_machine: dict[str, Placement],
    machines: dict[str, Machine],
    obstacles: set[Cell],
    region: CellBox,
) -> Route | Infeasibility:
    """Dock every machine route-aware and grow the net's shared-amperage cable tree.

    The source docks on whichever usable (non-front) face is nearest its first sink; its cell is
    the tree root (depth 0). Each sink, in net-endpoint order, then either **taps** the trunk - if
    one of its dock candidates is already a trunk cell, its terminal lands on that cell and no new
    cable is laid (GT: one cable block feeds every adjacent wired face; the shallowest such cell
    wins, candidate order breaking ties) - or **extends** the tree with a multi-goal A* leg from
    all trunk cells laid so far to any of its free dock cells (``dock_candidates`` +
    ``astar_multi``). The cell a leg reaches is the terminal, so routing - not a fixed face order
    - chooses the face. One exception keeps the route well-formed: a route with no segments fails
    validation, so the last sink never taps a still-segment-less trunk - it lays a real leg.

    Every trunk cell's **depth** is its cable-block distance from the source; each sink's load is
    then sized at its *delivered* voltage, so cable loss thickens the run (``_size_trunk``).
    Laid cells stay blocked for the legs that follow (a leg may *attach* at a trunk cell but never
    cross one), so the trunk is always a single tree the validator can root at the source. A
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
        # No extra docked-cell exclusion: this net's terminals may share trunk cells (taps), and
        # a finished net's trunk - every one of its terminals sits on a segment cell - is already
        # in ``obstacles`` by the time the next net docks.
        cand = dock_candidates(ep.port_id, placement, machine, obstacles, set(), region)
        if not cand:
            return _no_dock(net_id)
        candidates.append(cand)

    # Dock the source on the face nearest its first sink; its cell roots the tree at depth 0.
    source_terminal = _nearest(candidates[0], candidates[1])
    root = _cell(source_terminal)

    terminals: list[Terminal] = [source_terminal]
    legs: list[list[Cell]] = []
    depth: dict[Cell, int] = {root: 0}  # trunk cell -> cable-block distance from the source
    trunk: list[Cell] = [root]  # every trunk cell in laid order (deterministic A* seeding)
    sink_cells: list[Cell] = []  # sink_cells[i] = sink m_i's terminal cell
    blocked = obstacles | {root}  # grows with the trunk so legs never cross it

    for i, cand in enumerate(candidates[1:]):
        # A tap lays no cable and a zero-segment route fails validation (ROUTE_DISCONTINUOUS),
        # so the last sink must extend a trunk that has no segments yet.
        may_tap = bool(legs) or i < len(sinks) - 1
        taps = [t for t in cand if _cell(t) in depth] if may_tap else []
        if taps:
            tapped = min(taps, key=lambda t: depth[_cell(t)])  # shallowest; min() keeps cand order
            terminals.append(tapped)
            sink_cells.append(_cell(tapped))
            continue
        goals = {_cell(t) for t in cand} - blocked  # blocked already covers the laid trunk
        if not goals:
            return _no_dock(net_id)  # every usable face is taken by the trunk already laid
        path = astar_multi(trunk, goals, blocked, region)
        if path is None:
            return Infeasibility(
                constraint="routing",
                detail=f"power net {net_id!r}: no free cell path to a dock face of "
                f"{cand[0].machine_id!r}",
                suggested_relaxation="enlarge the bounding region or reduce obstacles",
            )
        for prev, cell in pairwise(path):  # path[0] is a trunk cell; the rest are new
            depth[cell] = depth[prev] + 1
            trunk.append(cell)
        blocked.update(path)  # later legs must avoid these cells so the graph stays a tree
        legs.append(path)
        end = path[-1]
        terminals.append(next(t for t in cand if _cell(t) == end))
        sink_cells.append(end)

    loads = [(machines[e.machine_id].eut, machines[e.machine_id].voltage_tier) for e in sinks]
    sized = _size_trunk(net_id, legs, depth, sink_cells, loads)
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
    depth: Mapping[Cell, int],
    sink_cells: Sequence[Cell],
    loads: Sequence[tuple[float, str]],
) -> tuple[list[Segment], list[int]] | Infeasibility:
    """Size each segment to the summed load of the sink terminals on its far-from-root side.

    ``loads[i]`` / ``sink_cells[i]`` are sink ``m_i``'s ``(eut, tier)`` and its terminal cell; its
    cable-block distance from the source is that cell's tree ``depth`` (a tap of the root is
    distance 0). Each sink's load is *fractional*, sized at its delivered voltage
    (``eut / (tier_voltage - distance)``, ``dataset.amp_load`` - machines buffer packets and
    average below a whole amp), so cable loss thickens the run instead of under-powering the far
    machines. Each segment then carries the total load of the sink terminals in the subtree
    hanging off its child end - the rooted-tree sum that defines a shared-amperage trunk -
    rounded up to whole amps only per segment (``whole_amps``): rounding per machine would
    overstate the draw. Sinks sharing one cell add up; a sink tapping the root loads no segment
    (it draws straight from the source's own cable block). Segments are emitted leg by leg in
    laid order, the thickness list aligned 1:1 - the validator re-derives all of this
    independently. A run whose delivered voltage reaches 0 (:class:`UnpowerableError`) or a
    segment whose summed load exceeds 16x is rejected, not silently certified.
    """
    amp_at: dict[Cell, float] = {}
    for (eut, tier), cell in zip(loads, sink_cells, strict=True):
        try:
            amp_at[cell] = amp_at.get(cell, 0.0) + amp_load(eut, tier, distance=depth[cell])
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

    # Subtree sums, leaves first: every leg is laid parent-before-child and only attaches to
    # cells laid before it, so walking the legs in reverse (each leg child-end first) folds every
    # cell's total into its parent exactly once.
    subtree = dict(amp_at)
    for path in reversed(legs):
        for parent, child in reversed(list(pairwise(path))):
            subtree[parent] = subtree.get(parent, 0.0) + subtree.get(child, 0.0)

    segments: list[Segment] = []
    thickness: list[int] = []
    for path in legs:
        for parent, child in pairwise(path):
            # Everything on the segment's far-from-root side, rounded to the whole packets
            # (amps) the cable must actually be rated for.
            amps = whole_amps(subtree.get(child, 0.0))
            if amps > MAX_CABLE_THICKNESS:
                return Infeasibility(
                    constraint="amperage",
                    detail=f"power net {net_id!r}: a cable segment must carry {amps} amps, over "
                    "the 16x cable cap",
                    suggested_relaxation="split into parallel runs or use a higher voltage tier "
                    "(more power per amp) - Phase 2 multi-source optimization",
                )
            segments.append(Segment(start=coord(parent), end=coord(child), channel=0))
            thickness.append(_cable_thickness(amps))
    return segments, thickness


def _nearest(candidates: Sequence[Terminal], targets: Sequence[Terminal]) -> Terminal:
    """The candidate terminal whose cell is closest (Manhattan) to any target cell.

    Docks the source facing its first sink; ties keep the earlier candidate (``FACE_ORDER``).
    """
    target_cells = [_cell(t) for t in targets]
    return min(candidates, key=lambda t: min(manhattan(_cell(t), tc) for tc in target_cells))


def _cell(terminal: Terminal) -> Cell:
    return terminal.cell.as_tuple()


def _cable_thickness(load: int) -> int:
    """Smallest cable thickness (1/2/4/8/16) that carries ``load`` amps (>=1 even for 0)."""
    for t in CABLE_THICKNESSES:
        if load <= t:
            return t
    return MAX_CABLE_THICKNESS  # the caller already rejected load > 16


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
