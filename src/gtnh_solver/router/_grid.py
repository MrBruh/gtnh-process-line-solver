"""Shared cell-grid primitives for the routers (generic + power).

Obstacle building, terminal docking on a usable (non-front) machine face, and A* between cells
all live here so ``router.core`` (item/fluid) and ``router.power`` route over the *same* grid
model with one implementation. The conventions (front face = placement orientation carries no
I/O; machine + reserved cells are obstacles; the validator independently re-checks every
terminal) are unchanged from the original crude router.
"""

from __future__ import annotations

import heapq
from collections.abc import Sequence

from gtnh_solver.ir import CellBox, CellCoord, Facing, InputIR, Machine, Placement, Terminal
from gtnh_solver.ir.geometry import FACE_DELTAS, Cell, in_region, occupied_cells

# Order to try non-front faces (south first -> docks into the open +z row). The front face
# (== placement orientation) is skipped at runtime.
FACE_ORDER = (Facing.SOUTH, Facing.NORTH, Facing.EAST, Facing.WEST, Facing.UP, Facing.DOWN)
NEIGHBORS = tuple(FACE_DELTAS.values())
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


def dock(
    port_id: str,
    placement: Placement,
    machine: Machine,
    obstacles: set[Cell],
    docked: set[Cell],
    region: CellBox,
) -> Terminal | None:
    """First free cell just outside a usable (non-front) face of the machine, as a Terminal."""
    body = set(occupied_cells(placement.cell, machine.footprint))
    for face in FACE_ORDER:
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


def astar(start: Cell, goal: Cell, obstacles: set[Cell], region: CellBox) -> list[Cell] | None:
    """Shortest in-bounds, obstacle-free cell path from ``start`` to ``goal`` (unit hops)."""
    heap: list[tuple[int, int, Cell]] = [(manhattan(start, goal), 0, start)]
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
        for dx, dy, dz in NEIGHBORS:
            nxt = (cur[0] + dx, cur[1] + dy, cur[2] + dz)
            if not in_region(nxt, region) or nxt in obstacles:
                continue
            ng = g + 1
            if ng < best.get(nxt, _UNREACHABLE):
                best[nxt] = ng
                came_from[nxt] = cur
                heapq.heappush(heap, (ng + manhattan(nxt, goal), ng, nxt))
    return None


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
