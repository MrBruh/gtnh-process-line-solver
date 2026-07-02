"""router.core - the Phase 1 crude per-commodity router.

Given placed machines, connect each non-ME **item/fluid** net (power is the power router's job,
``router.power``). The router is the geometry authority for *how* a net connects: it first
decides which nets GT's free **auto-output** connection covers (``auto.assign_auto_outputs`` -
adjacent 1-source-1-sink nets, one auto-output per machine) and lays pipes only for the rest.
For each piped net: resolve a :class:`~gtnh_solver.ir.Terminal` per endpoint (a free cell just
outside a usable, non-front machine face - the front comes from the placement orientation, so no
dataset is needed), then A* between the terminals over the free cell grid (machine + reserved
cells are obstacles). Routes are laid **capacity-aware**: each net's cells become obstacles for
the nets after it, so two nets never share a cell - the crude single-channel cap (one route per
cell), which the validator independently enforces. Because that makes the result order-dependent,
the router does **rip-up/reroute**: route a pass, and if any net failed, rip everything up and
retry with the failed nets first (most-constrained-first), until a pass is clean or the failed-net
set repeats (a genuine infeasibility, not an ordering accident). Crude on purpose
(docs/ROADMAP.md): one channel per cell; the per-edge multi-channel cap and negotiated-congestion
routing are later lane-D work (GitHub #7). The shared cell-grid primitives (obstacle building,
docking, A*) live in ``_grid`` so this router and ``router.power`` route over one grid model.

Returns the auto-connections plus the routes, or an explicit ``Infeasibility`` naming the net
that could not dock or route - never raises for the expected case, matching the placer/validator
discipline. The validator independently certifies that every terminal is on a non-front face
adjacent to its machine and lies on the route, and re-checks every auto-connection.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from gtnh_solver.ir import (
    AutoConnection,
    CellBox,
    CellCoord,
    Commodity,
    Infeasibility,
    InputIR,
    Machine,
    Net,
    Placement,
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.ir.geometry import Cell

from ._grid import astar, coord, dock, obstacle_cells
from .auto import assign_auto_outputs

#: Backstop on rip-up/reroute passes (cycle detection on the failed-net set usually stops first).
_MAX_PASSES = 16


@dataclass(frozen=True)
class RouteResult:
    """Crude router output: the auto-output vs pipe decision, or why routing stalled.

    ``auto_connections`` are the nets the router satisfied with GT's free auto-output instead of
    a pipe (the router owns that decision); ``routes`` are the pipes for the rest.
    ``failed_nets`` lists the nets left unrouted (empty when ``ok``), in problem order, so the
    solver's place<->route feedback loop can penalize exactly those nets and re-place.
    """

    routes: tuple[Route, ...] = ()
    infeasibility: Infeasibility | None = None
    failed_nets: tuple[str, ...] = ()
    auto_connections: tuple[AutoConnection, ...] = ()

    @property
    def ok(self) -> bool:
        """True iff every non-ME net was routed."""
        return self.infeasibility is None


def route(problem: InputIR, placements: Sequence[Placement]) -> RouteResult:
    """Connect each non-ME net of ``problem`` over the given placements: auto-output, then pipes.

    The router first decides, from the final placements + orientations, which nets a free
    auto-output connection covers (``auto.assign_auto_outputs``); only the uncovered nets are
    piped. Routing is capacity-aware (one route per cell), so the order nets are routed in
    matters - a net that grabs a scarce cell can wedge a later net out. So this does
    **rip-up/reroute**: route a pass in the current order, and if any net failed, rip everything
    up and retry with the failed nets moved to the front (most-constrained-first). A failed-net
    set already seen means reordering is cycling rather than progressing, so it stops and reports
    the failure - what is left is a genuine infeasibility, not an ordering accident. Crude: the
    per-edge multi-channel cap and negotiated-congestion routing are later lane-D work (GitHub #7).
    """
    autos, covered = assign_auto_outputs(problem, placements)
    auto_connections = tuple(autos)
    nets = [
        net
        for net in problem.nets
        if net.id not in covered  # satisfied by auto-output, no pipe needed
        and not problem.me_toggles.toggled(net.commodity)
        and net.commodity is not Commodity.POWER  # power is the power router's job (router.power)
    ]
    if not nets:
        return RouteResult(auto_connections=auto_connections)

    order = list(nets)
    seen_failed: set[frozenset[str]] = set()
    routes: list[Route] = []
    failures: dict[str, Infeasibility] = {}
    for _ in range(_MAX_PASSES):
        routes, failures = _route_pass(problem, placements, order)
        if not failures:
            return RouteResult(tuple(routes), auto_connections=auto_connections)
        key = frozenset(failures)
        if key in seen_failed:
            break  # this failed-net set already came up - reordering is cycling, not progressing
        seen_failed.add(key)
        # Rip everything up; give the nets that failed first pick next pass (most-constrained-first).
        failed_ids = set(failures)
        order = [n for n in nets if n.id in failed_ids] + [
            n for n in nets if n.id not in failed_ids
        ]

    # Exhausted: report the first net still failing (in original order), with its specific reason,
    # plus every still-failing net so the solver's feedback loop can penalize them all.
    still_failing = tuple(net.id for net in nets if net.id in failures)
    return RouteResult(tuple(routes), failures[still_failing[0]], still_failing, auto_connections)


def _route_pass(
    problem: InputIR, placements: Sequence[Placement], nets: Sequence[Net]
) -> tuple[list[Route], dict[str, Infeasibility]]:
    """Route ``nets`` once in the given order, capacity-aware, skipping (not aborting on) failures.

    Returns the routes laid plus, per net that could not be routed *given the cells the earlier
    nets in this order claimed*, why. Failures here are order-dependent - the caller retries with
    a different order. Caller owns net filtering (ME/power/auto-covered); this routes exactly
    what it is given.
    """
    machines = {m.id: m for m in problem.machines}
    placement_by_machine: dict[str, Placement] = {}
    for placement in placements:
        placement_by_machine.setdefault(placement.machine_id, placement)  # one placement/machine
    region = problem.bounding_region

    obstacles = obstacle_cells(problem, placements, machines)
    routes: list[Route] = []
    failures: dict[str, Infeasibility] = {}
    for net in nets:
        outcome = _route_one_net(net, placement_by_machine, machines, obstacles, region)
        if isinstance(outcome, Infeasibility):
            failures[net.id] = outcome
            continue
        routes.append(outcome)
        # Capacity: this net now owns these cells, so the nets after it route around them.
        obstacles.update(_segment_cells(outcome.segments))
    return routes, failures


def _route_one_net(
    net: Net,
    placement_by_machine: dict[str, Placement],
    machines: dict[str, Machine],
    obstacles: set[Cell],
    region: CellBox,
) -> Route | Infeasibility:
    """Dock a terminal per endpoint and A* between them over the free grid, or the reason it could
    not. Nothing is committed to ``obstacles`` here - the caller does that only on success, so a
    partly-docked failed net leaves no trace for the retry."""
    chosen: set[Cell] = set()  # this net's own terminals, so two of its ports don't co-locate
    terminals: list[Terminal] = []
    for endpoint in net.endpoints:
        ep_placement = placement_by_machine.get(endpoint.machine_id)
        ep_machine = machines.get(endpoint.machine_id)
        if ep_placement is None or ep_machine is None:
            return _no_dock(net.id, endpoint.machine_id)
        terminal = dock(endpoint.port_id, ep_placement, ep_machine, obstacles, chosen, region)
        if terminal is None:
            return _no_dock(net.id, endpoint.machine_id)
        chosen.add((terminal.cell.x, terminal.cell.y, terminal.cell.z))
        terminals.append(terminal)

    segments = _connect([t.cell for t in terminals], obstacles, region)
    if segments is None:
        return _no_path(net.id)
    return Route(net_id=net.id, commodity=net.commodity, terminals=terminals, segments=segments)


def _segment_cells(segments: Sequence[Segment]) -> set[Cell]:
    """Every cell a route's segments touch (both endpoints of each hop)."""
    cells: set[Cell] = set()
    for seg in segments:
        cells.add((seg.start.x, seg.start.y, seg.start.z))
        cells.add((seg.end.x, seg.end.y, seg.end.z))
    return cells


def _connect(
    cells: Sequence[CellCoord], obstacles: set[Cell], region: CellBox
) -> list[Segment] | None:
    """Chain consecutive terminals with A*; the union is a single connected subgraph."""
    segments: list[Segment] = []
    for a, b in pairwise(cells):
        path = astar((a.x, a.y, a.z), (b.x, b.y, b.z), obstacles, region)
        if path is None:
            return None
        for c0, c1 in pairwise(path):
            segments.append(Segment(start=coord(c0), end=coord(c1), channel=0))
    return segments


def _no_dock(net_id: str, machine_id: str) -> Infeasibility:
    return Infeasibility(
        constraint="face_reachability",
        detail=f"net {net_id!r} could not dock a terminal on machine {machine_id!r} "
        f"(no free non-front face cell)",
        suggested_relaxation="free up adjacent cells, or leave routing gaps around machines",
    )


def _no_path(net_id: str) -> Infeasibility:
    return Infeasibility(
        constraint="routing",
        detail=f"net {net_id!r} has no free cell path between its terminals",
        suggested_relaxation="enlarge the bounding region or reduce obstacles",
    )
