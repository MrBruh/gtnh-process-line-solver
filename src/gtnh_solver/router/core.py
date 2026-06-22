"""router.core - the Phase 1 crude per-commodity router.

Given placed machines, connect each non-ME net: resolve a :class:`~gtnh_solver.ir.Terminal`
per endpoint (a free cell just outside a usable, non-front machine face - the front comes from
the placement orientation, so no dataset is needed), then A* between the terminals over the
free cell grid (machine + reserved cells are obstacles). Crude on purpose (docs/ROADMAP.md):
one channel everywhere, no inter-net capacity, item/fluid only (power isn't synthesized yet).

Returns routes, or an explicit ``Infeasibility`` naming the net that could not dock or route -
never raises for the expected case, matching the placer/validator discipline. The validator
independently certifies that every terminal is on a non-front face adjacent to its machine and
lies on the route.
"""

from __future__ import annotations

import heapq
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from gtnh_solver.ir import (
    CellBox,
    CellCoord,
    Commodity,
    Facing,
    Infeasibility,
    InputIR,
    Machine,
    Placement,
    Route,
    Segment,
    Terminal,
)
from gtnh_solver.ir.geometry import FACE_DELTAS, Cell, in_region, occupied_cells

# Order the router tries non-front faces (south first -> docks into the open +z row). The front
# face (== placement orientation) is skipped at runtime.
_FACE_ORDER = (Facing.SOUTH, Facing.NORTH, Facing.EAST, Facing.WEST, Facing.UP, Facing.DOWN)
_NEIGHBORS = tuple(FACE_DELTAS.values())
_UNREACHABLE = 1 << 30


@dataclass(frozen=True)
class RouteResult:
    """Crude router output: all routes, or a partial set plus why it stalled."""

    routes: tuple[Route, ...] = ()
    infeasibility: Infeasibility | None = None

    @property
    def ok(self) -> bool:
        """True iff every non-ME net was routed."""
        return self.infeasibility is None


def route(problem: InputIR, placements: Sequence[Placement]) -> RouteResult:
    """Route every non-ME net of ``problem`` over the given ``placements``."""
    machines = {m.id: m for m in problem.machines}
    placement_by_machine: dict[str, Placement] = {}
    for placement in placements:
        placement_by_machine.setdefault(placement.machine_id, placement)  # count==1 in Phase 1
    region = problem.bounding_region

    obstacles: set[Cell] = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    for placement in placements:
        machine = machines.get(placement.machine_id)
        if machine is not None:
            obstacles.update(occupied_cells(placement.cell, machine.footprint))

    docked: set[Cell] = set()
    routes: list[Route] = []
    for net in problem.nets:
        if problem.me_toggles.toggled(net.commodity):
            continue

        terminals: list[Terminal] = []
        for endpoint in net.endpoints:
            ep_placement = placement_by_machine.get(endpoint.machine_id)
            ep_machine = machines.get(endpoint.machine_id)
            if ep_placement is None or ep_machine is None:
                return RouteResult(tuple(routes), _no_dock(net.id, endpoint.machine_id))
            terminal = _dock(endpoint.port_id, ep_placement, ep_machine, obstacles, docked, region)
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


def _dock(
    port_id: str,
    placement: Placement,
    machine: Machine,
    obstacles: set[Cell],
    docked: set[Cell],
    region: CellBox,
) -> Terminal | None:
    """First free cell just outside a usable (non-front) face of the machine, as a Terminal."""
    body = set(occupied_cells(placement.cell, machine.footprint))
    for face in _FACE_ORDER:
        if face is placement.orientation:  # front face carries no I/O
            continue
        dx, dy, dz = FACE_DELTAS[face]
        for bx, by, bz in sorted(body):
            cand = (bx + dx, by + dy, bz + dz)
            if cand in body or not in_region(cand, region) or cand in obstacles or cand in docked:
                continue
            return Terminal(
                machine_id=placement.machine_id,
                port_id=port_id,
                face=face,
                cell=CellCoord(x=cand[0], y=cand[1], z=cand[2]),
            )
    return None


def _connect(
    cells: Sequence[CellCoord], obstacles: set[Cell], region: CellBox
) -> list[Segment] | None:
    """Chain consecutive terminals with A*; the union is a single connected subgraph."""
    segments: list[Segment] = []
    for a, b in pairwise(cells):
        path = _astar((a.x, a.y, a.z), (b.x, b.y, b.z), obstacles, region)
        if path is None:
            return None
        for c0, c1 in pairwise(path):
            segments.append(Segment(start=_coord(c0), end=_coord(c1), channel=0))
    return segments


def _astar(start: Cell, goal: Cell, obstacles: set[Cell], region: CellBox) -> list[Cell] | None:
    heap: list[tuple[int, int, Cell]] = [(_manhattan(start, goal), 0, start)]
    came_from: dict[Cell, Cell] = {}
    best: dict[Cell, int] = {start: 0}
    visited: set[Cell] = set()
    while heap:
        _, g, cur = heapq.heappop(heap)
        if cur == goal:
            return _reconstruct(came_from, cur)
        if cur in visited:
            continue
        visited.add(cur)
        for dx, dy, dz in _NEIGHBORS:
            nxt = (cur[0] + dx, cur[1] + dy, cur[2] + dz)
            if not in_region(nxt, region) or nxt in obstacles:
                continue
            ng = g + 1
            if ng < best.get(nxt, _UNREACHABLE):
                best[nxt] = ng
                came_from[nxt] = cur
                heapq.heappush(heap, (ng + _manhattan(nxt, goal), ng, nxt))
    return None


def _reconstruct(came_from: dict[Cell, Cell], cur: Cell) -> list[Cell]:
    path = [cur]
    while cur in came_from:
        cur = came_from[cur]
        path.append(cur)
    path.reverse()
    return path


def _manhattan(a: Cell, b: Cell) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def _coord(c: Cell) -> CellCoord:
    return CellCoord(x=c[0], y=c[1], z=c[2])


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
