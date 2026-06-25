"""router.core - the Phase 1 crude per-commodity router.

Given placed machines, connect each non-ME net: resolve a :class:`~gtnh_solver.ir.Terminal`
per endpoint (a free cell just outside a usable, non-front machine face - the front comes from
the placement orientation, so no dataset is needed), then A* between the terminals over the
free cell grid (machine + reserved cells are obstacles). Crude on purpose (docs/ROADMAP.md):
one channel, no inter-net capacity. The shared cell-grid primitives (obstacle building, docking,
A*) live in ``_grid`` so this router and ``router.power`` route over one grid model.

Returns routes, or an explicit ``Infeasibility`` naming the net that could not dock or route -
never raises for the expected case, matching the placer/validator discipline. The validator
independently certifies that every terminal is on a non-front face adjacent to its machine and
lies on the route.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from itertools import pairwise

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    Infeasibility,
    InputIR,
    Placement,
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.ir.geometry import Cell

from ._grid import astar, coord, dock, obstacle_cells


@dataclass(frozen=True)
class RouteResult:
    """Crude router output: all routes, or a partial set plus why it stalled."""

    routes: tuple[Route, ...] = ()
    infeasibility: Infeasibility | None = None

    @property
    def ok(self) -> bool:
        """True iff every non-ME net was routed."""
        return self.infeasibility is None


def route(
    problem: InputIR, placements: Sequence[Placement], *, skip_nets: Collection[str] = ()
) -> RouteResult:
    """Route each non-ME net of ``problem`` (except ``skip_nets``) over the given placements."""
    machines = {m.id: m for m in problem.machines}
    placement_by_machine: dict[str, Placement] = {}
    for placement in placements:
        placement_by_machine.setdefault(placement.machine_id, placement)  # one placement/machine
    region = problem.bounding_region

    obstacles = obstacle_cells(problem, placements, machines)
    docked: set[Cell] = set()
    routes: list[Route] = []
    for net in problem.nets:
        if net.id in skip_nets or problem.me_toggles.toggled(net.commodity):
            continue

        terminals: list[Terminal] = []
        for endpoint in net.endpoints:
            ep_placement = placement_by_machine.get(endpoint.machine_id)
            ep_machine = machines.get(endpoint.machine_id)
            if ep_placement is None or ep_machine is None:
                return RouteResult(tuple(routes), _no_dock(net.id, endpoint.machine_id))
            terminal = dock(endpoint.port_id, ep_placement, ep_machine, obstacles, docked, region)
            if terminal is None:
                return RouteResult(tuple(routes), _no_dock(net.id, endpoint.machine_id))
            docked.add((terminal.cell.x, terminal.cell.y, terminal.cell.z))
            terminals.append(terminal)

        segments = _connect([t.cell for t in terminals], obstacles, region)
        if segments is None:
            return RouteResult(tuple(routes), _no_path(net.id))
        thickness = [1] * len(segments) if net.commodity is Commodity.POWER else None
        routes.append(
            Route(
                net_id=net.id,
                commodity=net.commodity,
                terminals=terminals,
                segments=segments,
                thickness_per_segment=thickness,
            )
        )
    return RouteResult(tuple(routes))


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
