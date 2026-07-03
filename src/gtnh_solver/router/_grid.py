"""Shared cell-grid primitives for the routers (generic + power).

Obstacle building, terminal docking on a usable (non-front) machine face, and A* between cells
all live here so ``router.core`` (item/fluid) and ``router.power`` route over the *same* grid
model with one implementation. The conventions (front face = placement orientation carries no
I/O; machine + reserved cells are obstacles; the validator independently re-checks every
terminal) are unchanged from the original crude router.
"""

from __future__ import annotations

import heapq
from collections.abc import Collection, Iterator, Mapping, Sequence

from gtnh_solver.ir import CellBox, CellCoord, Facing, InputIR, Machine, Placement, Terminal
from gtnh_solver.ir.geometry import FACE_DELTAS, FACE_OFFSETS, Cell, in_region, occupied_cells

# Order to try non-front faces (south first -> docks into the open +z row). The front face
# (== placement orientation) is skipped at runtime.
FACE_ORDER = (Facing.SOUTH, Facing.NORTH, Facing.EAST, Facing.WEST, Facing.UP, Facing.DOWN)
NEIGHBORS = FACE_OFFSETS  # the six face-adjacent unit steps A* expands into
_UNREACHABLE = 1 << 30


def obstacle_cells(
    problem: InputIR, placements: Sequence[Placement], machines: dict[str, Machine]
) -> set[Cell]:
    """Cells a route must avoid: reserved cells plus every placed machine's body."""
    obstacles: set[Cell] = {(c.x, c.y, c.z) for c in problem.reserved_cells}
    for placement in placements:
        machine = machines.get(placement.machine_id)
        if machine is not None:
            obstacles.update(occupied_cells(placement.cell, machine.footprint))
    return obstacles


def _dock_faces(
    port_id: str,
    placement: Placement,
    machine: Machine,
    obstacles: set[Cell],
    docked: set[Cell],
    region: CellBox,
) -> Iterator[Terminal]:
    """Free cells just outside the machine's usable (non-front) faces, one Terminal per face+cell.

    The single scan behind both :func:`dock` and :func:`dock_candidates`: it walks ``FACE_ORDER``
    (front face skipped) and, within each, ascending body cell, yielding a Terminal for every free,
    in-region, unclaimed cell - deduping a cell already yielded from an earlier face. ``dock`` takes
    the first yield; ``dock_candidates`` takes them all. The first cell yielded is exactly ``dock``'s
    old first free cell (the dedup only affects later faces), so neither result changes.
    """
    body = set(occupied_cells(placement.cell, machine.footprint))
    seen: set[Cell] = set()
    for face in FACE_ORDER:
        if face is placement.orientation:  # front face carries no I/O
            continue
        dx, dy, dz = FACE_DELTAS[face]
        for bx, by, bz in sorted(body):
            cand = (bx + dx, by + dy, bz + dz)
            if cand in body or cand in seen:
                continue
            if not in_region(cand, region) or cand in obstacles or cand in docked:
                continue
            seen.add(cand)
            yield Terminal(
                machine_id=placement.machine_id,
                port_id=port_id,
                face=face,
                cell=CellCoord(x=cand[0], y=cand[1], z=cand[2]),
            )


def dock(
    port_id: str,
    placement: Placement,
    machine: Machine,
    obstacles: set[Cell],
    docked: set[Cell],
    region: CellBox,
) -> Terminal | None:
    """First free cell just outside a usable (non-front) face of the machine, as a Terminal."""
    return next(_dock_faces(port_id, placement, machine, obstacles, docked, region), None)


def dock_candidates(
    port_id: str,
    placement: Placement,
    machine: Machine,
    obstacles: set[Cell],
    docked: set[Cell],
    region: CellBox,
) -> list[Terminal]:
    """Every free cell just outside a usable (non-front) face, one Terminal per face+cell.

    Where :func:`dock` commits to the first free face in ``FACE_ORDER`` (blind to where the route
    then has to go), this returns *all* the options so a route-aware caller (the power router) can
    dock on whichever face yields the shortest cable. Deterministic order: ``FACE_ORDER``, then
    ascending body cell.
    """
    return list(_dock_faces(port_id, placement, machine, obstacles, docked, region))


def astar(
    start: Cell,
    goal: Cell,
    obstacles: set[Cell],
    region: CellBox,
    cell_cost: Mapping[Cell, float] | None = None,
) -> list[Cell] | None:
    """Cheapest in-bounds, obstacle-free cell path from ``start`` to ``goal``.

    Each hop costs 1 plus the entered cell's ``cell_cost`` (0 where absent), so with no
    ``cell_cost`` this is the plain shortest path. The negotiated-congestion router prices
    contested cells through it: a priced cell is *discouraged*, never blocked - only
    ``obstacles`` are hard. Manhattan distance stays an admissible heuristic because every
    extra cost is non-negative on top of the unit base.
    """
    prices: Mapping[Cell, float] = cell_cost if cell_cost is not None else {}
    heap: list[tuple[float, float, Cell]] = [(float(manhattan(start, goal)), 0.0, start)]
    came_from: dict[Cell, Cell] = {}
    best: dict[Cell, float] = {start: 0.0}
    visited: set[Cell] = set()
    while heap:
        _, g, cur = heapq.heappop(heap)
        if cur == goal:
            return _reconstruct(came_from, cur)
        if cur in visited:
            continue
        visited.add(cur)
        for dx, dy, dz in NEIGHBORS:
            nxt = (cur[0] + dx, cur[1] + dy, cur[2] + dz)
            if not in_region(nxt, region) or nxt in obstacles:
                continue
            ng = g + 1 + prices.get(nxt, 0.0)
            if ng < best.get(nxt, _UNREACHABLE):
                best[nxt] = ng
                came_from[nxt] = cur
                heapq.heappush(heap, (ng + manhattan(nxt, goal), ng, nxt))
    return None


def astar_multi(
    starts: Collection[Cell], goals: set[Cell], obstacles: set[Cell], region: CellBox
) -> list[Cell] | None:
    """Shortest obstacle-free path from any cell in ``starts`` to any cell in ``goals``.

    Multi-source, multi-goal A* (the heuristic is the Manhattan distance to the nearest goal). The
    power router uses it to dock a cable on whichever usable face gives the shortest run: ``goals``
    are all of a machine's free non-front dock cells, so routing - not a fixed face order - picks
    the terminal. Like :func:`astar`, ``starts`` are seeded at cost 0 even if they lie in
    ``obstacles`` (a leg begins on the previous leg's end cell, already part of the laid trunk).
    Returns the path (``path[0] in starts``, ``path[-1] in goals``), or ``None`` if none is
    reachable. ``goals`` must be non-empty and disjoint from ``starts`` (a zero-length trunk is not
    a valid cable); the caller guarantees this.
    """
    if not goals:
        return None
    heap: list[tuple[int, int, Cell]] = [(_nearest_goal(s, goals), 0, s) for s in starts]
    heapq.heapify(heap)
    came_from: dict[Cell, Cell] = {}
    best: dict[Cell, int] = dict.fromkeys(starts, 0)
    visited: set[Cell] = set()
    while heap:
        _, g, cur = heapq.heappop(heap)
        if cur in goals:
            return _reconstruct(came_from, cur)
        if cur in visited:
            continue
        visited.add(cur)
        for dx, dy, dz in NEIGHBORS:
            nxt = (cur[0] + dx, cur[1] + dy, cur[2] + dz)
            if not in_region(nxt, region) or nxt in obstacles:
                continue
            ng = g + 1
            if ng < best.get(nxt, _UNREACHABLE):
                best[nxt] = ng
                came_from[nxt] = cur
                heapq.heappush(heap, (ng + _nearest_goal(nxt, goals), ng, nxt))
    return None


def _nearest_goal(cell: Cell, goals: set[Cell]) -> int:
    return min(manhattan(cell, g) for g in goals)


def _reconstruct(came_from: dict[Cell, Cell], cur: Cell) -> list[Cell]:
    path = [cur]
    while cur in came_from:
        cur = came_from[cur]
        path.append(cur)
    path.reverse()
    return path


def manhattan(a: Cell, b: Cell) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def coord(c: Cell) -> CellCoord:
    return CellCoord(x=c[0], y=c[1], z=c[2])
